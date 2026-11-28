from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Annotated, ClassVar, Self

from agent_comms.messaging.audience_manifest import AudiencedMessage
from agent_comms.messaging.bus_tables import BusRecipientRouteCounts, ReceivedSource
from agent_comms.messaging.bus_projection_tables import BusRouteIdentity
from agent_comms.messaging.messages import BusRow, MessageInput, PublicRecord
from agent_comms.messaging.page_traversal import PageTraversal
from agent_comms.messaging.projection_source import ProducedRow
from agent_comms.messaging.read_basis import ActorRoute, ReadCursor
from agent_comms.messaging.routing import DeliveryScope
from agent_comms.nominal.class_fact import ClassFact
from agent_comms.storage.typed_table import Column, ExpressionIndex, Index, TypedRow


@dataclass(frozen=True)
class BusRouteCounts(BusRouteIdentity):

    @ClassFact
    def writes(cls) -> tuple[type[ProducedRow], ...]:
        return (BusRouteCounts,)

    seq: Annotated[int, Column(primary_key=True)]
    timestamp: float
    indexes: ClassVar = (
        Index(("target", "sender", "sender_lookup", "seq")),
        Index(("target", "sender", "sender_lookup", "timestamp", "seq")),
    )

    @classmethod
    def from_message(cls, message: PublicRecord | AudiencedMessage):
        return cls(message.target, message.sender, message.sender_lookup,
                   message.seq, message.timestamp)

    @classmethod
    def has_target_since(
        cls, db: sqlite3.Connection, after_seq: int, through: int, targets: Iterable[str],
    ) -> bool:
        """Whether any delivered row in (after_seq, through] addressed one of ``targets``."""
        target_list = tuple(targets)
        if through <= after_seq or not target_list:
            return False
        placeholders = ",".join("?" for _ in target_list)
        return db.execute(
            f'SELECT 1 FROM "{cls.declared_name}" '
            f'WHERE seq > ? AND seq <= ? AND target IN ({placeholders}) LIMIT 1',
            (after_seq, through, *target_list),
        ).fetchone() is not None


@dataclass(frozen=True)
class BusRouteKeys(BusRouteIdentity):
    """Each route a message took, once, with the first message that took it.

    A read asks which routes reach its targets. Deriving that per read crossed every registered
    sender with every target (41,000 index probes for 762 routes on the live bus, 11 of a watch
    read's 20 ms); the route is a fact of the write, so it is kept there."""

    @ClassFact
    def writes(cls: type[BusRouteKeys]) -> tuple[type[ProducedRow], ...]:
        return (BusRouteKeys,)

    first_seq: int
    indexes: ClassVar = (
        ExpressionIndex(("{target}", "{sender}", "coalesce({sender_lookup}, '')"), unique=True),
        Index(("sender", "sender_lookup", "first_seq", "target")),
    )
    """A public sender has no lookup and stores NULL, which no primary key holds and every
    NULL-distinct UNIQUE admits twice; the expression makes it one route like any other."""

    @classmethod
    def from_message(cls, message: PublicRecord | AudiencedMessage) -> Self:
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
            f'''WITH target AS MATERIALIZED (
                SELECT value AS target FROM json_each(?)
            ), sender AS MATERIALIZED (
                SELECT s.key AS sender,l.value AS sender_lookup
                FROM json_each(?) AS s,json_each(s.value) AS l
            )
            SELECT DISTINCT route.sender
            FROM sender
            CROSS JOIN "{cls.declared_name}" AS route
                ON route.sender=sender.sender AND route.sender_lookup IS sender.sender_lookup
                    AND route.first_seq<=?
            CROSS JOIN target ON target.target=route.target''',
            (json.dumps(targets), json.dumps(dict(senders)), through))]


@dataclass(frozen=True)
class RouteUnread(ActorRoute, TypedRow):
    count: int


@dataclass(frozen=True)
class UnseenDelivery(TypedRow):
    seq: int
    target: str


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
class HeldRouteRead:
    """A route answer through one sequence interval, under its actual query inputs."""

    inputs: RouteInputs = RouteInputs()
    through: int = 0


@dataclass(frozen=True)
class HeldUnseenCounts(HeldRouteRead):
    """Unseen counts per actor and conversation through one bus revision."""

    counts: Mapping[str, Mapping[str, int]] = field(default_factory=dict)


class RouteCounts(ABC):
    """Bus readers share delivery queries and their existing observing lifetime."""

    @abstractmethod
    def observing(self) -> AbstractContextManager[sqlite3.Connection]:
        """Borrow one read snapshot through the reader's declared lifetime."""

    _route_sources = f'''arguments AS MATERIALIZED (
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
        FROM arguments
        CROSS JOIN sender_keys AS sender
        CROSS JOIN "{BusRouteKeys.declared_name}" AS route
            ON route.sender=sender.sender AND route.sender_lookup IS sender.sender_lookup
                AND route.first_seq<=arguments.through
        CROSS JOIN request_targets AS target ON target.target=route.target
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
    ), actor_all_floor AS MATERIALIZED (
        SELECT actor,max(high_water) AS hw FROM actor_floor WHERE target='' GROUP BY actor
    ), actor_target_floor AS MATERIALIZED (
        SELECT target.actor,target.target,
            max(coalesce(target_floor.high_water,0),coalesce(all_floor.hw,0)) AS hw
        FROM actor_targets AS target
        LEFT JOIN actor_floor AS target_floor
            ON target_floor.actor=target.actor AND target_floor.target=target.target
        LEFT JOIN actor_all_floor AS all_floor ON all_floor.actor=target.actor
    ), actor_min_floor AS MATERIALIZED (
        SELECT recipient.key AS actor,recipient.value AS lookup,
            coalesce(min(floor.hw),0) AS min_hw,viewer.since AS viewer_since
        FROM recipients AS recipient
        JOIN addressed_since AS viewer ON viewer.name=recipient.key
        LEFT JOIN actor_target_floor AS floor ON floor.actor=recipient.key
        GROUP BY recipient.key
    ), addressed_rows AS MATERIALIZED (
        SELECT row.seq,row.target,row.sender,row.timestamp,alias.canonical
        FROM "{BusRouteCounts.declared_name}" AS row
        JOIN route_keys AS route ON route.target=row.target
            AND route.sender=row.sender AND route.sender_lookup IS row.sender_lookup
        LEFT JOIN aliases AS alias ON alias.name=row.sender
        WHERE row.seq>(SELECT after FROM arguments) AND row.seq<=(SELECT through FROM arguments)
            AND row.timestamp>=route.since
    )'''

    _requests = f'''requests AS NOT MATERIALIZED (
        SELECT viewer_floor.actor,row.seq,row.target,row.sender
        FROM actor_min_floor AS viewer_floor
        CROSS JOIN "{ReceivedSource.declared_name}" AS addressed
            ON addressed.lookup=viewer_floor.lookup
                AND addressed.seq>max(viewer_floor.min_hw,(SELECT after FROM arguments))
                AND addressed.seq<=(SELECT through FROM arguments)
        CROSS JOIN addressed_rows AS row ON row.seq=addressed.seq
        CROSS JOIN actor_target_floor AS target_floor
            ON target_floor.actor=viewer_floor.actor AND target_floor.target=row.target
        WHERE row.seq>target_floor.hw
            AND row.timestamp>=viewer_floor.viewer_since
            AND {DeliveryScope.delivers_sql("viewer_floor.actor", "row.sender", "row.canonical")}
        UNION ALL
        SELECT route.actor,row.seq,row.target,row.sender
        FROM route_since AS route
        CROSS JOIN actor_target_floor AS target_floor
            ON target_floor.actor=route.actor AND target_floor.target=route.target
        CROSS JOIN "{BusRouteCounts.declared_name}" AS row ON row.target=route.target
            AND row.sender=route.sender AND row.sender_lookup IS route.sender_lookup
        WHERE route.unaddressed IS NOT NULL AND row.timestamp>=route.unaddressed
            AND row.seq>max(target_floor.hw,(SELECT after FROM arguments))
            AND row.seq<=(SELECT through FROM arguments)
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
                                    parameters: tuple[object, ...] = (), *,
                                    requests: str = _requests) -> list[Row]:
        return row_type.read(db.execute(
            f'''WITH {ReadCursor.sql("?")}, {ReadCursor.floor_sql},
            {self._route_sources}, {requests}, {ReadCursor.joined_sql} {query}''',
            (*inputs.bindings(through, after), *parameters)))

    def _route_rows[Row: TypedRow](self, row_type: type[Row], registry,
                                  deliveries: Mapping[str, DeliveryScope], seen: Mapping[str, ReadCursor],
                                  after: int, query: str,
                                  parameters: tuple[object, ...] = ()) -> list[Row]:
        with self.observing() as db:
            through = BusRow.last_seq(db)
            inputs = self._route_inputs(db, registry, deliveries, seen, through)
            return self._route_query(db, row_type, inputs, through, after, query, parameters)

    _summary_unseen_sql = f'''rc_dm AS MATERIALIZED (
        SELECT actor,target,sender,high_water FROM read_cursors WHERE sender!=''
    ), actor_routes AS MATERIALIZED (
        SELECT tf.actor,vf.lookup,rk.target,rk.sender,rk.sender_lookup,
               coalesce(rk.sender_lookup,'') AS sl,
               max(tf.hw,coalesce(dm.high_water,0),(SELECT after FROM arguments)) AS min_seq,
               max(vf.viewer_since,rk.since) AS min_ts
        FROM actor_target_floor AS tf
        JOIN actor_min_floor AS vf ON vf.actor=tf.actor
        JOIN route_keys AS rk ON rk.target=tf.target
        LEFT JOIN aliases AS alias ON alias.name=rk.sender
        LEFT JOIN rc_dm AS dm ON dm.actor=tf.actor AND dm.target=tf.target AND dm.sender=rk.sender
        WHERE {DeliveryScope.delivers_sql("tf.actor", "rk.sender", "alias.canonical")}
    ), recipient_routes AS MATERIALIZED (
        SELECT ar.actor,ar.lookup,ar.target,ar.sender,ar.min_seq,ar.min_ts,
               br.sender_lookup,br.deliveries,br.min_seq AS r_min_seq,
               br.max_seq AS r_max_seq,br.min_timestamp AS r_min_ts
        FROM actor_routes AS ar
        CROSS JOIN "{BusRecipientRouteCounts.declared_name}" AS br
            ON br.lookup=ar.lookup AND br.target=ar.target
                AND br.sender=ar.sender AND br.sender_lookup IS ar.sender_lookup
                AND br.max_seq>ar.min_seq
        WHERE ar.min_seq<(SELECT through FROM arguments)
    ), half1_whole AS (
        SELECT actor,target,sender,deliveries AS cnt
        FROM recipient_routes
        WHERE r_min_seq>min_seq
            AND r_max_seq<=(SELECT through FROM arguments)
            AND r_min_ts>=min_ts
    ), half1_split AS (
        SELECT rr.actor,rr.target,rr.sender,count(*) AS cnt
        FROM recipient_routes AS rr
        CROSS JOIN "{BusRouteCounts.declared_name}" AS row
            ON row.target=rr.target AND row.sender=rr.sender
                AND row.sender_lookup IS rr.sender_lookup
                AND row.seq>rr.min_seq AND row.seq<=(SELECT through FROM arguments)
        WHERE NOT (rr.r_min_seq>rr.min_seq
                AND rr.r_max_seq<=(SELECT through FROM arguments)
                AND rr.r_min_ts>=rr.min_ts)
            AND row.timestamp>=rr.min_ts
            AND EXISTS (SELECT 1 FROM "{ReceivedSource.declared_name}" AS addressed
                WHERE addressed.lookup=rr.lookup AND addressed.seq=row.seq)
        GROUP BY rr.actor,rr.target,rr.sender
    ), half2_raw AS (
        SELECT route.actor,row.target,row.sender,count(*) AS cnt
        FROM route_since AS route
        CROSS JOIN actor_target_floor AS tf
            ON tf.actor=route.actor AND tf.target=route.target
        LEFT JOIN rc_dm AS dm
            ON dm.actor=route.actor AND dm.target=route.target AND dm.sender=route.sender
        CROSS JOIN "{BusRouteCounts.declared_name}" AS row ON row.target=route.target
            AND row.sender=route.sender AND row.sender_lookup IS route.sender_lookup
        WHERE route.unaddressed IS NOT NULL AND row.timestamp>=route.unaddressed
            AND row.seq>max(tf.hw,coalesce(dm.high_water,0),(SELECT after FROM arguments))
            AND row.seq<=(SELECT through FROM arguments)
            AND NOT EXISTS (SELECT 1 FROM "{ReceivedSource.declared_name}" AS addressed
                WHERE addressed.lookup=route.recipient_lookup AND addressed.seq=row.seq)
        GROUP BY route.actor,row.target,row.sender
    ), ex_addressed AS (
        SELECT ex.ex_actor AS actor,row.target,row.sender,-count(*) AS cnt
        FROM cursor_exceptions AS ex
        CROSS JOIN "{BusRouteCounts.declared_name}" AS row ON row.seq=ex.ex_seq
        CROSS JOIN actor_routes AS ar
            ON ar.actor=ex.ex_actor AND ar.target=row.target
                AND ar.sender=row.sender AND ar.sl=coalesce(row.sender_lookup,'')
        WHERE row.target=ex.ex_target
            AND (ex.ex_sender='' OR ex.ex_sender=row.sender)
            AND ex.ex_seq>ar.min_seq AND ex.ex_seq<=(SELECT through FROM arguments)
            AND row.timestamp>=ar.min_ts
            AND EXISTS (SELECT 1 FROM "{ReceivedSource.declared_name}" AS addressed
                WHERE addressed.lookup=ar.lookup AND addressed.seq=ex.ex_seq)
        GROUP BY ex.ex_actor,row.target,row.sender
    ), ex_unaddressed AS (
        SELECT ex.ex_actor AS actor,row.target,row.sender,-count(*) AS cnt
        FROM route_since AS rs
        CROSS JOIN cursor_exceptions AS ex
            ON ex.ex_actor=rs.actor AND ex.ex_target=rs.target
                AND (ex.ex_sender='' OR ex.ex_sender=rs.sender)
        CROSS JOIN "{BusRouteCounts.declared_name}" AS row ON row.seq=ex.ex_seq
        CROSS JOIN actor_target_floor AS tf
            ON tf.actor=rs.actor AND tf.target=rs.target
        LEFT JOIN rc_dm AS dm
            ON dm.actor=rs.actor AND dm.target=rs.target AND dm.sender=rs.sender
        WHERE rs.unaddressed IS NOT NULL
            AND row.target=rs.target AND row.sender=rs.sender
            AND row.sender_lookup IS rs.sender_lookup
            AND row.timestamp>=rs.unaddressed
            AND ex.ex_seq>max(tf.hw,coalesce(dm.high_water,0),(SELECT after FROM arguments))
            AND ex.ex_seq<=(SELECT through FROM arguments)
            AND NOT EXISTS (SELECT 1 FROM "{ReceivedSource.declared_name}" AS addressed
                WHERE addressed.lookup=rs.recipient_lookup AND addressed.seq=ex.ex_seq)
        GROUP BY ex.ex_actor,row.target,row.sender
    )
    SELECT actor,target,sender,sum(cnt) AS count
    FROM (
        SELECT * FROM half1_whole
        UNION ALL SELECT * FROM half1_split
        UNION ALL SELECT * FROM half2_raw
        UNION ALL SELECT * FROM ex_addressed
        UNION ALL SELECT * FROM ex_unaddressed
    )
    GROUP BY actor,target,sender
    HAVING sum(cnt)>0'''

    def summary_unseen_counts(
        self, db: sqlite3.Connection, inputs: RouteInputs, through: int, after: int,
    ) -> list[RouteUnread]:
        return RouteUnread.read(db.execute(
            f'''WITH {ReadCursor.sql("?")}, {ReadCursor.floor_sql},
            {self._route_sources}, {self._summary_unseen_sql}''',
            inputs.bindings(through, after),
        ))

    def per_row_unseen_counts(
        self, db: sqlite3.Connection, inputs: RouteInputs, through: int, after: int,
        unread_rule: str,
    ) -> list[RouteUnread]:
        return self._route_query(
            db, RouteUnread, inputs, through, after,
            f'''SELECT actor,target,sender,count(*) AS count FROM cursors AS request
                WHERE {unread_rule} GROUP BY actor,target,sender''',
        )

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
        cursor = ReadCursor.counts_cursor(seen.values())
        with self.observing() as db:
            through = BusRow.last_seq(db)
            inputs = self._route_inputs(db, registry, deliveries, seen, through)
            rows = cursor.unseen_counts(self, db, inputs, through, after)
        return self._grouped(deliveries, rows, {})

    def advanced_unseen_counts(self, held: HeldUnseenCounts, registry,
                               deliveries: Mapping[str, DeliveryScope], seen: Mapping[str, ReadCursor]) -> HeldUnseenCounts:
        """The unseen counts through this reader's revision, counting only the rows appended
        since ``held`` when every value the count reads is unchanged. A delivered row's
        audience is frozen at append, so an older row's count cannot change under equal inputs."""
        cursor = ReadCursor.counts_cursor(seen.values())
        with self.observing() as db:
            through = BusRow.last_seq(db)
            inputs = self._route_inputs(db, registry, deliveries, seen, through)
            if inputs == held.inputs and held.through == through:
                return held
            start = held if inputs == held.inputs and held.through <= through else HeldUnseenCounts(inputs)
            rows = cursor.unseen_counts(self, db, inputs, through, start.through)
            return HeldUnseenCounts(inputs, through, self._grouped(deliveries, rows, start.counts))

    def unseen_deliveries(self, registry, delivery, seen, *, after: int = 0, scope=None, limit: int = 100,
                          traversal: PageTraversal):
        if limit <= 0:
            raise ValueError("Unread delivery limit must be positive.")
        scope = delivery if scope is None else scope
        predicate, values = scope.index_predicate
        rendered = MessageInput.render(predicate)
        return tuple(self._route_rows(UnseenDelivery, registry, {delivery.actor: delivery},
            {delivery.actor: seen}, after,
            f''', unread AS (
                SELECT request.seq,request.target,request.sender,{MessageInput.render('message.{mentions}')}
                FROM cursors AS request CROSS JOIN "{MessageInput.table.declared_name}" AS message USING(seq)
                WHERE actor=? AND {seen.unread_rule()}
            ) SELECT seq,target FROM unread WHERE ({rendered}) AND (? IS NULL OR seq < ?)
            ORDER BY seq {traversal.seq_order} LIMIT ?''',
            (delivery.actor, *values, traversal.upper, traversal.upper, limit)))
