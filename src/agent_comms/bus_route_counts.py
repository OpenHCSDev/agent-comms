"""Current delivery counts query derived rows in the bus's own read snapshot."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, ClassVar

from .bus_tables import ReceivedSource
from .class_fact import ClassFact
from .field_codec import ThreadLookup
from .messages import BusProjectionTables, BusRow, Message, PublicRecord, PublicSender
from .projection_source import ProducedRow
from .read_basis import ReadCursor
from .typed_table import Column, Index, TypedRow

if TYPE_CHECKING:
    from .bus_publication import Delivered
    from .page_traversal import PageTraversal


@dataclass(frozen=True)
class BusRouteCounts(BusProjectionTables):

    @ClassFact
    def writes(cls) -> tuple[type[ProducedRow], ...]:
        return (BusRouteCounts,)

    seq: Annotated[int, Column(primary_key=True)]
    target: str
    sender: str
    sender_lookup: ThreadLookup | PublicSender
    timestamp: float
    indexes: ClassVar = (
        Index(("target", "sender", "sender_lookup", "seq")),
        Index(("target", "sender", "sender_lookup", "timestamp", "seq")),
    )

    @classmethod
    def from_message(cls, message: PublicRecord | Delivered):
        return cls(message.seq, message.target, message.sender,
                   message.sender_lookup, message.timestamp)


@dataclass(frozen=True)
class RouteUnread(TypedRow):
    actor: str
    target: str
    sender: str
    count: int


@dataclass(frozen=True)
class UnseenDelivery(TypedRow):
    seq: int
    target: str


class RouteCounts:
    """Bus readers share delivery queries and their existing observing lifetime."""

    _requests = f'''arguments AS MATERIALIZED (
        SELECT ? AS targets,? AS recipients,? AS senders,? AS unrecorded,? AS through,? AS after,
            ? AS births,? AS aliases,? AS unaddressed
    ), births AS MATERIALIZED (
        SELECT member.key AS name,member.value AS since
        FROM arguments,json_each(arguments.births) AS member
    ), aliases AS MATERIALIZED (
        SELECT member.key AS name,member.value AS canonical
        FROM arguments,json_each(arguments.aliases) AS member
    ), unaddressed_since AS MATERIALIZED (
        SELECT member.key AS name,member.value AS since
        FROM arguments,json_each(arguments.unaddressed) AS member
    ), actor_targets AS MATERIALIZED (
        SELECT owner.key AS actor,target.value AS target
        FROM arguments,json_each(arguments.targets) AS owner,json_each(owner.value) AS target
    ), request_targets AS MATERIALIZED (
        SELECT DISTINCT target FROM actor_targets
    ), sender_keys AS MATERIALIZED (
        SELECT sender.key AS sender,lookup.value AS sender_lookup,birth.since
        FROM arguments,json_each(arguments.senders) AS sender,json_each(sender.value) AS lookup
        JOIN births AS birth ON birth.name=sender.key
    ), unrecorded AS MATERIALIZED (
        SELECT target.target,source.value AS sender,
            (SELECT sender_lookup FROM "{BusRouteCounts.declared_name}" AS route
             WHERE route.target=target.target AND route.sender=source.value
                 AND route.seq<=(SELECT through FROM arguments) AND route.sender_lookup IS NOT NULL
             ORDER BY route.seq LIMIT 1) AS sender_lookup
        FROM request_targets AS target,arguments,json_each(arguments.unrecorded) AS source
    ), route_keys AS MATERIALIZED (
        SELECT DISTINCT row.target,row.sender,row.sender_lookup,sender.since
        FROM request_targets AS target
        CROSS JOIN "{BusRouteCounts.declared_name}" AS row ON row.target=target.target
        JOIN sender_keys AS sender ON sender.sender=row.sender
            AND sender.sender_lookup IS row.sender_lookup
        WHERE row.seq>(SELECT after FROM arguments) AND row.seq<=(SELECT through FROM arguments)
        UNION ALL
        SELECT route.*,birth.since FROM unrecorded AS route
        JOIN births AS birth ON birth.name=route.sender WHERE route.sender_lookup IS NOT NULL
    ), route_since AS MATERIALIZED (
        SELECT target.actor,recipient.value AS recipient_lookup,
            route.target,route.sender,route.sender_lookup,
            max(route.since,unaddressed.since) AS unaddressed,
            max(route.since,viewer.since) AS addressed
        FROM actor_targets AS target
        JOIN route_keys AS route ON route.target=target.target
        JOIN births AS viewer ON viewer.name=target.actor
        CROSS JOIN arguments
        JOIN json_each(arguments.recipients) AS recipient ON recipient.key=target.actor
        LEFT JOIN aliases AS alias ON alias.name=route.sender
        LEFT JOIN unaddressed_since AS unaddressed ON unaddressed.name=target.actor
        WHERE coalesce(alias.canonical,route.sender)<>target.actor
    ), requests AS MATERIALIZED (
        SELECT route.actor,row.seq,row.target,row.sender
        FROM route_since AS route
        CROSS JOIN "{ReceivedSource.declared_name}" AS addressed
            ON addressed.lookup=route.recipient_lookup
        CROSS JOIN "{BusRouteCounts.declared_name}" AS row ON row.seq=addressed.seq
        WHERE row.target=route.target AND row.sender=route.sender
            AND row.sender_lookup IS route.sender_lookup AND row.timestamp>=route.addressed
            AND row.seq>(SELECT after FROM arguments) AND row.seq<=(SELECT through FROM arguments)
            AND {ReadCursor.above_floor("row.seq", "route.actor", "row.target")}
        UNION ALL
        SELECT route.actor,row.seq,row.target,row.sender
        FROM route_since AS route
        CROSS JOIN "{BusRouteCounts.declared_name}" AS row ON row.target=route.target
            AND row.sender=route.sender AND row.sender_lookup IS route.sender_lookup
        WHERE route.unaddressed IS NOT NULL AND row.timestamp>=route.unaddressed
            AND row.seq>(SELECT after FROM arguments) AND row.seq<=(SELECT through FROM arguments)
            AND {ReadCursor.above_floor("row.seq", "route.actor", "row.target")}
            AND NOT EXISTS (SELECT 1 FROM "{ReceivedSource.declared_name}" AS addressed
                WHERE addressed.lookup=route.recipient_lookup AND addressed.seq=row.seq)
    )'''

    def _route_rows(self, row_type, registry, deliveries, seen, after: int, query: str, parameters=()):
        with self.observing() as db:
            namespace = registry.sender_bindings
            bindings = (
                *ReadCursor.sql_bindings(registry,
                    {actor: seen[actor].counted(delivery, registry)
                     for actor, delivery in deliveries.items()}).values(),
                json.dumps({actor: sorted(delivery.index_targets)
                            for actor, delivery in deliveries.items()}),
                json.dumps({actor: registry.require(actor).incarnation.require_recorded().lookup
                            for actor in deliveries}),
                namespace.senders, namespace.unrecorded, BusRow.last_seq(db), after,
                namespace.births, json.dumps(dict(registry.aliases)),
                json.dumps({actor: registry.unread_since(
                    actor, registry.require(actor).incarnation.wire_birth, False,
                ) for actor in deliveries}),
                *parameters,
            )
            return row_type.read(db.execute(
                f'''WITH {ReadCursor.sql("?")}, {ReadCursor.floor_sql},
                {self._requests}, {ReadCursor.joined_sql} {query}''',
                bindings))

    def unseen_counts(self, registry, deliveries, seen, *, after: int = 0):
        counts = {actor: {} for actor in deliveries}
        rows = self._route_rows(RouteUnread, registry, deliveries, seen, after,
            f'''SELECT actor,target,sender,count(*) AS count FROM cursors
            WHERE {ReadCursor.unread_sql} GROUP BY actor,target,sender''')
        for row in rows:
            conversation = deliveries[row.actor].conversation(row.sender, row.target)
            counts[row.actor][conversation] = counts[row.actor].get(conversation, 0) + row.count
        return counts

    def unseen_deliveries(self, registry, delivery, seen, *, after: int = 0, scope=None, limit: int = 100,
                          traversal: PageTraversal):
        if limit <= 0:
            raise ValueError("Unread delivery limit must be positive.")
        scope = delivery if scope is None else scope
        predicate, values = scope.index_predicate
        return tuple(self._route_rows(UnseenDelivery, registry, {delivery.actor: delivery},
            {delivery.actor: seen}, after,
            f''', unread AS (
                SELECT request.seq,request.target,request.sender,message.mentions
                FROM cursors AS request CROSS JOIN "{Message.table.declared_name}" AS message USING(seq)
                WHERE actor=? AND {ReadCursor.unread_sql}
            ) SELECT seq,target FROM unread WHERE ({predicate}) AND (? IS NULL OR seq < ?)
            ORDER BY seq {traversal.seq_order} LIMIT ?''',
            (delivery.actor, *values, traversal.upper, traversal.upper, limit)))
