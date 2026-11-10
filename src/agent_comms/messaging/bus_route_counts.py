from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Annotated, ClassVar, Self

from agent_comms.messaging.bus_publication import Delivered
from agent_comms.messaging.bus_tables import ReceivedSource
from agent_comms.messaging.message_sender import PublicSender
from agent_comms.messaging.messages import BusProjectionTables, BusRow, MessageInput, PublicRecord
from agent_comms.messaging.page_traversal import PageTraversal
from agent_comms.messaging.projection_source import ProducedRow
from agent_comms.messaging.read_basis import ReadCursor
from agent_comms.messaging.routing import DeliveryScope
from agent_comms.nominal.class_fact import ClassFact
from agent_comms.nominal.field_codec import ThreadLookup
from agent_comms.storage.typed_table import Column, ExpressionIndex, Index, TypedRow


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
class BusRouteKeys(BusProjectionTables):
    """Each route a message took, once, with the first message that took it.

    A read asks which routes reach its targets. Deriving that per read crossed every registered
    sender with every target (41,000 index probes for 762 routes on the live bus, 11 of a watch
    read's 20 ms); the route is a fact of the write, so it is kept there."""

    @ClassFact
    def writes(cls: type[BusRouteKeys]) -> tuple[type[ProducedRow], ...]:
        return (BusRouteKeys,)

    target: str
    sender: str
    sender_lookup: ThreadLookup | PublicSender
    first_seq: int
    indexes: ClassVar = (
        ExpressionIndex(("{target}", "{sender}", "coalesce({sender_lookup}, '')"), unique=True),
        Index(("target", "sender", "sender_lookup", "first_seq")),
    )
    """A public sender has no lookup and stores NULL, which no primary key holds and every
    NULL-distinct UNIQUE admits twice; the expression makes it one route like any other."""

    @classmethod
    def from_message(cls, message: PublicRecord | Delivered) -> Self:
        return cls(message.target, message.sender, message.sender_lookup, message.seq)

    def insertion(self) -> tuple[str, tuple]:
        """Messages are projected in sequence order, so the first insert of a route keeps its
        first message and every later one leaves it as it is."""
        statement, values = super().insertion()
        return statement + " ON CONFLICT DO NOTHING", values

    @classmethod
    def senders_reaching(
        cls, db: sqlite3.Connection, targets: list[str], through: int, *,
        senders: Mapping[str, str],
    ) -> list[str]:
        """Probe only captured sender identities, never retired route names or births."""
        return [row[0] for row in db.execute(
            f'''SELECT DISTINCT route.sender
            FROM json_each(?) AS sender
            CROSS JOIN json_each(sender.value) AS lookup
            CROSS JOIN json_each(?) AS target
            CROSS JOIN "{cls.declared_name}" AS route
                ON route.target=target.value AND route.sender=sender.key
                    AND route.sender_lookup IS lookup.value
            WHERE route.first_seq<=?''',
            (json.dumps(dict(senders)), json.dumps(targets), through))]


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


@dataclass(frozen=True)
class RouteOldest(TypedRow):
    actor: str
    oldest: float


@dataclass(frozen=True)
class RouteInputs:
    """Every value a route count binds besides its sequence interval, around the interval's
    place in the query's arguments. Two counts with equal inputs count the same rows the same
    way."""

    leading: tuple = ()
    trailing: tuple = ()

    def bindings(self, through: int, after: int) -> tuple:
        return (*self.leading, through, after, *self.trailing)


@dataclass(frozen=True)
class HeldUnseenCounts:
    """Unseen counts per actor and conversation through one bus revision, with the inputs they
    were counted under. The empty inputs match no real count, so the first count is whole."""

    inputs: RouteInputs = RouteInputs()
    through: int = 0
    counts: Mapping[str, Mapping[str, int]] = field(default_factory=dict)


class RouteCounts(ABC):
    """Bus readers share delivery queries and their existing observing lifetime."""

    @abstractmethod
    def observing(self) -> AbstractContextManager[sqlite3.Connection]:
        """Borrow one read snapshot through the reader's declared lifetime."""

    _requests = f'''arguments AS MATERIALIZED (
        SELECT ? AS targets,? AS recipients,? AS senders,? AS unrecorded,? AS through,? AS after,
            ? AS births,? AS aliases,? AS unaddressed,? AS addressed
    ), recipients AS MATERIALIZED (
        SELECT member.key,member.value
        FROM arguments,json_each(arguments.recipients) AS member
    ), births AS MATERIALIZED (
        SELECT member.key AS name,member.value AS since
        FROM arguments,json_each(arguments.births) AS member
    ), aliases AS MATERIALIZED (
        SELECT member.key AS name,member.value AS canonical
        FROM arguments,json_each(arguments.aliases) AS member
    ), unaddressed_since AS MATERIALIZED (
        SELECT member.key AS name,member.value AS since
        FROM arguments,json_each(arguments.unaddressed) AS member
    ), addressed_since AS MATERIALIZED (
        SELECT member.key AS name,member.value AS since
        FROM arguments,json_each(arguments.addressed) AS member
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
        SELECT route.target,route.sender,route.sender_lookup,sender.since
        FROM arguments CROSS JOIN sender_keys AS sender
        CROSS JOIN request_targets AS target
        CROSS JOIN "{BusRouteKeys.declared_name}" AS route ON route.target=target.target
            AND route.sender=sender.sender AND route.sender_lookup IS sender.sender_lookup
            AND route.first_seq<=arguments.through
        WHERE arguments.after=0 OR EXISTS (SELECT 1 FROM "{BusRouteCounts.declared_name}" AS row
            WHERE row.target=route.target AND row.sender=route.sender
                AND row.sender_lookup IS route.sender_lookup
                AND row.seq>arguments.after AND row.seq<=arguments.through)
        UNION ALL
        SELECT route.*,birth.since FROM unrecorded AS route
        JOIN births AS birth ON birth.name=route.sender WHERE route.sender_lookup IS NOT NULL
    ), route_since AS MATERIALIZED (
        SELECT target.actor,recipient.value AS recipient_lookup,
            route.target,route.sender,route.sender_lookup,
            max(route.since,unaddressed.since) AS unaddressed
        FROM actor_targets AS target
        JOIN route_keys AS route ON route.target=target.target
        JOIN recipients AS recipient ON recipient.key=target.actor
        LEFT JOIN aliases AS alias ON alias.name=route.sender
        JOIN unaddressed_since AS unaddressed ON unaddressed.name=target.actor
            AND unaddressed.since IS NOT NULL
        WHERE {DeliveryScope.delivers_sql("target.actor", "route.sender", "alias.canonical")}
    ), requests AS NOT MATERIALIZED (
        SELECT recipient.key AS actor,row.seq,row.target,row.sender
        FROM route_keys AS route
        CROSS JOIN actor_targets AS target ON target.target=route.target
        CROSS JOIN recipients AS recipient ON recipient.key=target.actor
        CROSS JOIN "{BusRouteCounts.declared_name}" AS row ON row.target=route.target
            AND row.sender=route.sender AND row.sender_lookup IS route.sender_lookup
            AND row.seq>(SELECT after FROM arguments) AND row.seq<=(SELECT through FROM arguments)
        CROSS JOIN "{ReceivedSource.declared_name}" AS addressed
            ON addressed.lookup=recipient.value AND addressed.seq=row.seq
                AND {ReadCursor.above_floor("addressed.seq", "recipient.key", "''")}
        CROSS JOIN addressed_since AS viewer ON viewer.name=recipient.key
        LEFT JOIN aliases AS alias ON alias.name=row.sender
        WHERE row.timestamp>=max(route.since,viewer.since)
            AND {DeliveryScope.delivers_sql("recipient.key", "row.sender", "alias.canonical")}
            AND row.seq>(SELECT after FROM arguments) AND row.seq<=(SELECT through FROM arguments)
            AND {ReadCursor.above_floor("row.seq", "recipient.key", "row.target")}
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

    @staticmethod
    def _route_inputs(db: sqlite3.Connection, registry, deliveries: Mapping[str, DeliveryScope],
                      seen: Mapping[str, ReadCursor], through: int) -> RouteInputs:
        targets = sorted({target for delivery in deliveries.values()
                          for target in delivery.index_targets})
        bindings = registry.sender_bindings
        senders, unrecorded, births = bindings.bound(
            BusRouteKeys.senders_reaching(db, targets, through, senders=bindings.senders))
        return RouteInputs((
            *ReadCursor.sql_bindings(registry,
                {actor: seen[actor].counted(delivery, registry)
                 for actor, delivery in deliveries.items()}).values(),
            json.dumps({actor: sorted(delivery.index_targets)
                        for actor, delivery in deliveries.items()}),
            json.dumps({actor: registry.require(actor).incarnation.require_recorded().lookup
                        for actor in deliveries}),
            senders, unrecorded,
        ), (
            births, json.dumps(dict(registry.aliases)),
            json.dumps({actor: registry.unread_since(
                actor, registry.require(actor).incarnation.wire_birth, False,
            ) for actor in deliveries}),
            json.dumps({actor: registry.unread_since(
                actor, registry.require(actor).incarnation.wire_birth, True,
            ) for actor in deliveries}),
        ))

    def _route_query[Row: TypedRow](self, db: sqlite3.Connection, row_type: type[Row],
                                    inputs: RouteInputs, through: int, after: int, query: str,
                                    parameters: tuple[object, ...] = ()) -> list[Row]:
        return row_type.read(db.execute(
            f'''WITH {ReadCursor.sql("?")}, {ReadCursor.floor_sql},
            {self._requests}, {ReadCursor.joined_sql} {query}''',
            (*inputs.bindings(through, after), *parameters)))

    def _route_rows[Row: TypedRow](self, row_type: type[Row], registry,
                                  deliveries: Mapping[str, DeliveryScope], seen: Mapping[str, ReadCursor],
                                  after: int, query: str,
                                  parameters: tuple[object, ...] = ()) -> list[Row]:
        with self.observing() as db:
            through = BusRow.last_seq(db)
            inputs = self._route_inputs(db, registry, deliveries, seen, through)
            return self._route_query(db, row_type, inputs, through, after, query, parameters)

    @staticmethod
    def _unseen_query(seen: Mapping[str, ReadCursor]) -> str:
        return (f'''SELECT actor,target,sender,count(*) AS count FROM cursors AS request
            WHERE {ReadCursor.rule_of(seen.values())} GROUP BY actor,target,sender''')

    @staticmethod
    def _grouped(deliveries: Mapping[str, DeliveryScope], rows: Iterable[RouteUnread],
                 counted: Mapping[str, Mapping[str, int]]) -> dict[str, dict[str, int]]:
        counts = {actor: dict(counted.get(actor, {})) for actor in deliveries}
        for row in rows:
            conversation = deliveries[row.actor].conversation(row.sender, row.target)
            counts[row.actor][conversation] = counts[row.actor].get(conversation, 0) + row.count
        return counts

    def unseen_counts(self, registry, deliveries: Mapping[str, DeliveryScope], seen: Mapping[str, ReadCursor], *,
                      after: int = 0) -> dict[str, dict[str, int]]:
        rows = self._route_rows(RouteUnread, registry, deliveries, seen, after,
                                self._unseen_query(seen))
        return self._grouped(deliveries, rows, {})

    def advanced_unseen_counts(self, held: HeldUnseenCounts, registry,
                               deliveries: Mapping[str, DeliveryScope], seen: Mapping[str, ReadCursor]) -> HeldUnseenCounts:
        """The unseen counts through this reader's revision, counting only the rows appended
        since ``held`` when every value the count reads is unchanged. A delivered row's
        audience is frozen at append, so an older row's count cannot change under equal inputs."""
        with self.observing() as db:
            through = BusRow.last_seq(db)
            inputs = self._route_inputs(db, registry, deliveries, seen, through)
            start = held if inputs == held.inputs else HeldUnseenCounts(inputs)
            rows = self._route_query(db, RouteUnread, inputs, through, start.through,
                                     self._unseen_query(seen))
            return HeldUnseenCounts(inputs, through, self._grouped(deliveries, rows, start.counts))

    def unseen_oldest(self, registry, deliveries: Mapping[str, DeliveryScope],
                      seen: Mapping[str, ReadCursor]) -> dict[str, float]:
        """When each actor's oldest message under its cursor was sent, in one read."""
        rows = self._route_rows(RouteOldest, registry, deliveries, seen, 0,
            f'''SELECT request.actor AS actor,min(message.timestamp) AS oldest
            FROM cursors AS request
            CROSS JOIN "{MessageInput.table.declared_name}" AS message USING(seq)
            WHERE {ReadCursor.rule_of(seen.values())} GROUP BY request.actor''')
        return {row.actor: row.oldest for row in rows}

    def unseen_deliveries(self, registry, delivery, seen, *, after: int = 0, scope=None, limit: int = 100,
                          traversal: PageTraversal):
        if limit <= 0:
            raise ValueError("Unread delivery limit must be positive.")
        scope = delivery if scope is None else scope
        predicate, values = scope.index_predicate
        return tuple(self._route_rows(UnseenDelivery, registry, {delivery.actor: delivery},
            {delivery.actor: seen}, after,
            f''', unread AS (
                SELECT request.seq,request.target,request.sender,{MessageInput.render('message.{mentions}')}
                FROM cursors AS request CROSS JOIN "{MessageInput.table.declared_name}" AS message USING(seq)
                WHERE actor=? AND {seen.unread_rule()}
            ) SELECT seq,target FROM unread WHERE ({predicate}) AND (? IS NULL OR seq < ?)
            ORDER BY seq {traversal.seq_order} LIMIT ?''',
            (delivery.actor, *values, traversal.upper, traversal.upper, limit)))
