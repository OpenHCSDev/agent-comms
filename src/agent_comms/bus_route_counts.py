"""Current delivery counts query derived rows in the bus's own read snapshot."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, ClassVar

from .bus_tables import BusProjectionTables, BusRow, ReceivedSource
from .messages import Message
from .read_basis import ReadCursor
from .typed_table import Column, Index, TypedRow, TypedTable


@dataclass(frozen=True)
class BusRouteCounts(TypedTable, BusProjectionTables):
    seq: Annotated[int, Column(primary_key=True)]
    target: str
    sender: str
    sender_lookup: str
    timestamp: float
    indexes: ClassVar = (
        Index(("target", "sender", "sender_lookup", "seq")),
        Index(("target", "sender", "sender_lookup", "timestamp", "seq")),
    )

    @classmethod
    def producer_classes(cls, row) -> tuple[type, ...]:
        from .bus_publication import Delivered

        return (Message, Delivered)

    @classmethod
    def from_message(cls, message: Message):
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
        SELECT ? AS targets,? AS recipients,? AS senders,? AS unrecorded,? AS through,? AS after
    ), request_targets AS MATERIALIZED (
        SELECT DISTINCT target.value AS target
        FROM arguments,json_each(arguments.targets) AS owner,json_each(owner.value) AS target
    ), unrecorded AS MATERIALIZED (
        SELECT target.target,source.value AS sender,
            (SELECT sender_lookup FROM "{BusRouteCounts.declared_name}" AS route
             WHERE route.target=target.target AND route.sender=source.value
                 AND route.seq<=(SELECT through FROM arguments) AND route.sender_lookup<>''
             ORDER BY route.seq LIMIT 1) AS sender_lookup
        FROM request_targets AS target,arguments,json_each(arguments.unrecorded) AS source
    ), route_keys AS (
        SELECT target.target,sender.key AS sender,lookup.value AS sender_lookup
        FROM request_targets AS target,arguments,json_each(arguments.senders) AS sender,
            json_each(sender.value) AS lookup
        UNION ALL
        SELECT * FROM unrecorded WHERE sender_lookup IS NOT NULL
    ), candidates AS MATERIALIZED (
        SELECT owner.key AS actor,row.*,
            delivery_since(owner.key,row.sender,row.target,row.sender_lookup,addressed.seq) AS since
        FROM arguments,json_each(arguments.targets) AS owner,json_each(owner.value) AS target
        JOIN json_each(arguments.recipients) AS recipient ON recipient.key=owner.key
        JOIN route_keys AS route ON route.target=target.value
        JOIN "{BusRouteCounts.declared_name}" AS row ON row.target=route.target
            AND row.sender=route.sender AND row.sender_lookup=route.sender_lookup
        LEFT JOIN "{ReceivedSource.declared_name}" AS addressed
            ON addressed.lookup=recipient.value AND addressed.seq=row.seq
        WHERE row.seq>(SELECT after FROM arguments) AND row.seq<=(SELECT through FROM arguments)
    ), requests AS MATERIALIZED (
        SELECT * FROM candidates WHERE since IS NOT NULL AND timestamp>=since
    )'''

    def _route_rows(self, row_type, registry, deliveries, seen, after: int, query: str, parameters=()):
        with self.observing() as db:
            db.create_function(
                "delivery_since", 5,
                lambda actor, sender, target, lookup, received: registry.unread_since(
                    actor, deliveries[actor].minimum_timestamp(sender, target, lookup, registry),
                    bool(received)), deterministic=True,
            )
            senders = registry.sender_bindings
            bindings = (
                json.dumps({actor: sorted(delivery.index_targets)
                            for actor, delivery in deliveries.items()}),
                json.dumps({actor: registry.require(actor).incarnation.require_recorded().lookup
                            for actor in deliveries}),
                senders["senders"], senders["unrecorded"], BusRow.last_seq(db), after,
                *ReadCursor.sql_bindings(registry,
                    {actor: seen[actor].counted(delivery, registry)
                     for actor, delivery in deliveries.items()}).values(),
                *parameters,
            )
            return row_type.read(db.execute(
                f'WITH {self._requests}, {ReadCursor.sql("?")}, {ReadCursor.joined_sql} {query}',
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

    def unseen_deliveries(self, registry, delivery, seen, *, after: int = 0, scope=None, limit: int = 100):
        if limit <= 0:
            raise ValueError("Unread delivery limit must be positive.")
        scope = delivery if scope is None else scope
        predicate, values = scope.index_predicate
        return tuple(self._route_rows(UnseenDelivery, registry, {delivery.actor: delivery},
            {delivery.actor: seen}, after,
            f''', unread AS (
                SELECT request.seq,request.target,request.sender,message.mentions
                FROM cursors AS request JOIN "{Message.table.declared_name}" AS message USING(seq)
                WHERE actor=? AND {ReadCursor.unread_sql}
            ) SELECT seq,target FROM unread WHERE ({predicate}) ORDER BY seq LIMIT ?''',
            (delivery.actor, *values, limit)))
