"""Disposable, declaration-owned route counts over the canonical bus.

The bus owns timestamps and publication identities. This index stores their
projections, advances across complete appended rows, and is reset at migration.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, NoReturn

from .coordination_database import CoordinationStore
from .coordination_errors import CoordinationReadUnavailable
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .private_path import FileRevision
from .read_basis import ReadCursor
from .routing import DeliveryMessage, DeliveryScope
from .store_files import _store_lock
from .typed_table import Column, ForeignKey, Index, SQLiteJournalMode, TypedRow, TypedTable

if TYPE_CHECKING:
    from .registry_provenance import RegistryProvenance
    from .wire_log import WireSnapshot


class RouteTable:
    """Disposable tables owned and reset together by the route projection."""

    schema: ClassVar[int] = 6


@dataclass(frozen=True, kw_only=True)
class RouteTimeNode(DeclaredFamily, affix="RouteTimeNode"):
    """Immutable timestamp rank at one original route prefix.

    Compress equal leading bits, retaining at most one fork per IEEE-754 bit.
    Older roots never change when a route acquires later deliveries.
    """

    count: int
    key_width: ClassVar[int] = 64
    bit: int

    @staticmethod
    def key(timestamp: float) -> int:
        # Numeric timestamp comparison identifies both signed zero values.
        bits, = struct.unpack(">Q", struct.pack(">d", timestamp if timestamp else 0.0))
        sign = 1 << (RouteTimeNode.key_width - 1)
        return bits ^ ((sign * 2 - 1) if bits & sign else sign)

    @property
    @abstractmethod
    def minimum(self) -> float: ...

    @property
    @abstractmethod
    def maximum(self) -> float: ...

    @property
    @abstractmethod
    def children(self) -> tuple[int, ...]: ...

    def count_since(self, db, since: float) -> int:
        if self.minimum >= since:
            return self.count
        if self.maximum < since:
            return 0
        return sum(RouteTimeRow.require(db, child).node.count_since(db, since) for child in self.children)

    def add(self, db, original: RouteTimeRow, leaf: LeafRouteTimeNode):
        different = self.key(self.minimum) ^ self.key(leaf.timestamp)
        bit = self.key_width - different.bit_length()
        if bit < self.bit:
            added = RouteTimeRow.store(db, leaf)
            left, right = ((added, original) if leaf.timestamp < self.minimum
                           else (original, added))
            return ForkRouteTimeNode.join(db, bit, left, right), None
        return self.grow(db, leaf)

    @abstractmethod
    def grow(self, db, leaf: LeafRouteTimeNode): ...


@dataclass(frozen=True, kw_only=True)
class LeafRouteTimeNode(RouteTimeNode):
    timestamp: float
    head_seq: int
    bit: ClassVar[int] = RouteTimeNode.key_width

    @property
    def minimum(self):
        return self.timestamp

    @property
    def maximum(self):
        return self.timestamp

    @property
    def children(self):
        return ()

    def grow(self, db, leaf):
        return RouteTimeRow.store(db, replace(leaf, count=self.count + 1)), self.head_seq


@dataclass(frozen=True, kw_only=True)
class ForkRouteTimeNode(RouteTimeNode):
    lower: float
    upper: float
    left: int
    right: int

    @property
    def minimum(self):
        return self.lower

    @property
    def maximum(self):
        return self.upper

    @property
    def children(self):
        return (self.left, self.right)

    @classmethod
    def join(cls, db, bit, left: RouteTimeRow, right: RouteTimeRow):
        return RouteTimeRow.store(db, cls(
            count=left.node.count + right.node.count, bit=bit,
            lower=left.node.minimum, upper=right.node.maximum,
            left=left.id, right=right.id,
        ))

    def grow(self, db, leaf):
        left = RouteTimeRow.require(db, self.left)
        right = RouteTimeRow.require(db, self.right)
        if self.key(leaf.timestamp) & (1 << (self.key_width - self.bit - 1)):
            right, previous = right.node.add(db, right, leaf)
        else:
            left, previous = left.node.add(db, left, leaf)
        return self.join(db, self.bit, left, right), previous


@dataclass(frozen=True)
class RouteTimeRow(RouteTable, TypedTable):
    id: int = field(metadata={"sql": Column(primary_key=True)})
    node: RouteTimeNode

    @classmethod
    def store(cls, db, node: RouteTimeNode):
        # SQLite owns the fresh row identity; the declaration owns its encoding.
        cursor = cls.insert_values(db, node=node)
        return cls(cursor.lastrowid, node)

    @classmethod
    def require(cls, db, identity):
        node = cls.one(db, id=identity)
        if node is None:
            raise RelationViolationError("Route timestamp root has a missing child")
        return node


class RouteSourceRow(TypedRow, ABC):
    @classmethod
    def current(cls, connection) -> RouteSourceRow:
        rows = RecordedRouteSourceRow.select(connection)
        if rows:
            recorded, = rows
            return recorded
        return UnrecordedRouteSource()

    @property
    @abstractmethod
    def through(self) -> int: ...

    @abstractmethod
    def matches_source(self, source: WireSnapshot) -> bool: ...

    @abstractmethod
    def needs_rebuild(self, source: WireSnapshot) -> bool: ...

    @abstractmethod
    def check_source(self, source: WireSnapshot, preparation: RouteIndexState) -> None: ...


@dataclass(frozen=True)
class RecordedRouteSourceRow(RouteTable, RouteSourceRow, TypedTable, declared_name="route_source"):
    id: int = field(metadata={"sql": Column(primary_key=True)})
    revision: FileRevision
    tail_digest: str

    @property
    def through(self):
        return self.revision.size

    def matches_source(self, source):
        return (source.same_file(self.revision.identity)
                and source.fingerprint(self.through) == self.tail_digest)

    def needs_rebuild(self, source):
        # An empty older cut cannot retire a later committed projection.
        return source.boundary != 0 and not self.matches_source(source)

    def check_source(self, source, preparation):
        if not self.matches_source(source):
            preparation.defer_read(0, source.boundary)


@dataclass(frozen=True)
class UnrecordedRouteSource(RouteSourceRow):
    through: ClassVar[int] = 0

    def matches_source(self, source):
        return False

    def needs_rebuild(self, source):
        return True

    def check_source(self, source, preparation):
        pass  # No indexed deliveries can disagree with this reader's source.


@dataclass(frozen=True)
class RouteEntryRow(RouteTable, TypedTable):
    seq: int = field(metadata={"sql": Column(primary_key=True)})
    target: str
    sender: str
    sender_lookup: str
    timestamp: float
    time_root: int
    previous_at_timestamp: int | None
    indexes: ClassVar = (Index(("target", "sender", "sender_lookup", "seq")),)
    foreign_keys: ClassVar = (ForeignKey(
        ("time_root",), RouteTimeRow, ("id",), deferred=True,
    ),)

    @classmethod
    def record(cls, db, delivery: DeliveryMessage):
        message = delivery.message
        route = message.target, message.sender, delivery.sender_lookup
        roots = RouteTimeRow.read(db.execute(
            f'SELECT tree.* FROM "{RouteTimeRow.declared_name}" AS tree '
            f'WHERE tree.id=(SELECT time_root FROM "{cls.declared_name}" '
            'WHERE target=? AND sender=? AND sender_lookup=? ORDER BY seq DESC LIMIT 1)',
            route,
        ))
        leaf = LeafRouteTimeNode(count=1, timestamp=message.timestamp, head_seq=message.seq)
        if roots:
            original, = roots
            root, previous = original.node.add(db, original, leaf)
        else:
            root, previous = RouteTimeRow.store(db, leaf), None
            RouteIdentityRow(*route, message.seq).insert(db)
        cls(message.seq, *route, message.timestamp, root.id, previous).insert(db)


@dataclass(frozen=True)
class RouteIdentityRow(RouteTable, TypedTable):
    target: str = field(metadata={"sql": Column(primary_key=True)})
    sender: str = field(metadata={"sql": Column(primary_key=True)})
    sender_lookup: str = field(metadata={"sql": Column(primary_key=True)})
    first_seq: int
    without_rowid: ClassVar = True
    indexes: ClassVar = (Index(("target", "sender", "first_seq", "sender_lookup")),)


@dataclass(frozen=True)
class RouteUnread(TypedRow):
    actor: str
    target: str
    sender: str
    since: float
    full: RouteTimeNode
    prefix: RouteTimeNode | None
    painted: int

    def count(self, connection: sqlite3.Connection) -> int:
        return (self.full.count_since(connection, self.since)
                - (self.prefix.count_since(connection, self.since) if self.prefix is not None else 0)
                - self.painted)


@dataclass(frozen=True)
class UnseenDelivery(TypedRow):
    seq: int
    target: str


@dataclass(frozen=True)
class RouteIndexState:
    """How far one writer sync left the committed index behind its cut."""

    complete: ClassVar[bool]

    def defer_read(self, through: int, size: int) -> NoReturn:
        raise RouteIndexRebuilding(through, size)


@dataclass(frozen=True)
class RouteIndexReady(RouteIndexState):
    complete: ClassVar[bool] = True


@dataclass(frozen=True)
class RouteIndexBehind(RouteIndexState):
    complete: ClassVar[bool] = False
    through: int
    size: int


@dataclass(frozen=True)
class RouteIndexBlocked(RouteIndexState):
    """Another writer holds the lock; this call could not make progress."""

    complete: ClassVar[bool] = False

    def defer_read(self, through: int, size: int) -> NoReturn:
        raise CoordinationReadUnavailable("Route projection writer is busy")


class RouteIndexRebuilding(CoordinationReadUnavailable):
    """The committed index is too far behind this cut to read its remainder."""

    def __init__(self, through: int, size: int) -> None:
        self.through = through
        self.size = size
        super().__init__(f"Route index covers {through} of {size} bytes")


class BusRouteCounts:
    rebuild_bytes = 4 * 1024 * 1024

    def __init__(self, bus_path: Path):
        self.bus_path = bus_path
        self.path = bus_path.with_name(f"bus_route_counts.v{RouteTable.schema}.sqlite3")

    def __enter__(self) -> BusRouteCounts:
        """Open one committed WAL snapshot without taking the writer lock.

        A WAL reader still meets SQLite's own short busy windows (checkpoint,
        WAL reset, recovery after the last connection closed); it waits them
        out through SQLite's busy handler instead of failing the read.
        """
        with CoordinationReadUnavailable.checking():
            return self._acquire()

    def _acquire(self) -> BusRouteCounts:
        self.connection = self._open()
        return self

    def _open(self) -> sqlite3.Connection:
        self._initialize()
        return CoordinationStore.read_snapshot(self.path)

    def __exit__(self, _error_type, _error, _traceback) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        """The writer alone creates the WAL store; no source bytes are decoded here."""
        if self.path.exists():
            with closing(CoordinationStore.read_snapshot(self.path)) as current:
                if (RecordedRouteSourceRow.present(current)
                        and SQLiteJournalMode.read(current.execute("PRAGMA journal_mode"))[0].journal_mode == "wal"):
                    return
        with _store_lock(self.path), closing(CoordinationStore.writer(self.path)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            if not RecordedRouteSourceRow.present(connection):
                for owner in TypedTable.members_with(RouteTable):
                    owner.create(connection)
            connection.commit()

    def prepare(self) -> None:
        """Create the writer-owned WAL store after its publication lock releases."""
        self._initialize()

    def sync(
        self, source: WireSnapshot,
        decode: Callable[[Mapping], tuple[DeliveryMessage, ...]],
        *, blocking: bool = True,
    ) -> RouteIndexState:
        """Decode a bounded source delta first, then commit under a short lock.

        A reader passes ``blocking=False``: when another writer holds the
        projection lock it is committing the same bytes, so this reader
        answers from its own remainder instead of waiting.
        """
        size = source.boundary
        with closing(self._open()) as connection:
            recorded = RouteSourceRow.current(connection)
        rebuild = recorded.needs_rebuild(source)
        offset = 0 if rebuild else recorded.through
        if not rebuild and offset >= size:
            return RouteIndexReady()

        through = offset
        rows = []
        for record, _, through in source.rows(offset, min(size, offset + self.rebuild_bytes)):
            rows.extend(decode(record))
        through = source.position

        try:
            with (
                _store_lock(self.path, blocking=blocking),
                closing(CoordinationStore.writer(self.path)) as connection,
            ):
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                current = RouteSourceRow.current(connection)
                if current != recorded:
                    connection.rollback()
                    return (RouteIndexReady() if current.through >= size
                            else RouteIndexBehind(current.through, size))
                if rebuild:
                    for owner in TypedTable.members_with(RouteTable):
                        owner.delete(connection)
                for row in rows:
                    RouteEntryRow.record(connection, row)
                RecordedRouteSourceRow.delete(connection)
                if through:
                    RecordedRouteSourceRow(1, replace(source.revision, size=through), source.fingerprint(through)).insert(connection)
                connection.commit()
        except BlockingIOError:
            return RouteIndexBlocked()
        return RouteIndexReady() if through == size else RouteIndexBehind(through, size)


class OpenedRouteCounts(BusRouteCounts):
    def __init__(
        self, bus_path: Path, source: WireSnapshot,
        registry: RegistryProvenance, deliveries: Mapping[str, DeliveryScope],
    ):
        super().__init__(bus_path)
        self.source = source
        self.registry = registry
        self.deliveries = deliveries
        self._channel_members: dict[str, set[str]] = {}
        for actor, delivery in deliveries.items():
            for target in delivery.channels:
                self._channel_members.setdefault(target, set()).add(actor)

    def _decode(self, record: Mapping) -> tuple[DeliveryMessage, ...]:
        return tuple(DeliveryMessage.from_wire(record, root_id=self.source.metadata.root_id))

    def _prepare(self) -> None:
        self._preparation = self.sync(self.source, self._decode, blocking=False)

    def _acquire(self) -> OpenedRouteCounts:
        self._prepare()
        super()._acquire()
        try:
            self._appended = self._appended_deliveries()
            # SQLite borrows the original routing behavior. Do not decode all
            # route rows into Python requests and encode them back into SQL.
            self.connection.create_function(
                "delivery_since", 4,
                lambda actor, sender, target, lookup: self.deliveries[actor].minimum_timestamp(
                    sender, target, lookup, self.registry
                ), deterministic=True,
            )
        except BaseException:
            self.connection.close()
            raise
        return self

    def _appended_deliveries(self) -> tuple[DeliveryMessage, ...]:
        """The bounded part of this cut still missing from the SQLite snapshot."""
        size = self.source.boundary
        if size == 0:
            return ()
        recorded = RouteSourceRow.current(self.connection)
        through = recorded.through
        recorded.check_source(self.source, self._preparation)
        if size - through > self.rebuild_bytes:
            self._preparation.defer_read(through, size)
        if through >= size:
            return ()
        return tuple(row for record, _, _ in self.source.rows(through, size)
                     for row in self._decode(record))

    def _recipients(self, target: str):
        direct = self.registry.canonical_name(target)
        return self._channel_members.get(
            target, {direct} if direct in self.deliveries else set()
        )

    def unseen_counts(self, seen: Mapping[str, ReadCursor]) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {actor: {} for actor in self.deliveries}

        def add(actor: str, sender: str, target: str, count: int):
            if count:
                conversation = self.deliveries[actor].conversation(sender, target)
                counts[actor][conversation] = counts[actor].get(conversation, 0) + count

        for row in self._read_counts(seen):
            add(row.actor, row.sender, row.target, row.count(self.connection))
        for item in self._appended:
            message = item.message
            for actor in self._recipients(message.target):
                if self.deliveries[actor].unread(item, self.registry, seen[actor]):
                    add(actor, message.sender, message.target, 1)
        return counts

    def unseen_deliveries(self, actor: str, seen: ReadCursor, *, after: int = 0) -> tuple[UnseenDelivery, ...]:
        delivery = self.deliveries[actor]
        return (*self._read_deliveries(actor, seen, after), *(
            UnseenDelivery(item.message.seq, item.message.target)
            for item in self._appended
            if item.message.seq > after and delivery.unread(item, self.registry, seen)
        ))

    _requests = f"""request_targets AS MATERIALIZED (
                SELECT DISTINCT target.value AS target
                FROM json_each(:targets) AS owner,json_each(owner.value) AS target
            ), unrecorded AS MATERIALIZED (
                SELECT target.target,source.value AS sender,
                    (SELECT sender_lookup FROM "{RouteIdentityRow.declared_name}" AS route
                     WHERE route.target=target.target AND route.sender=source.value
                         AND route.first_seq<=:through AND route.sender_lookup<>''
                     ORDER BY route.first_seq LIMIT 1) AS sender_lookup
                FROM request_targets AS target,json_each(:unrecorded) AS source
            ), route_keys AS (
                SELECT target.target,sender.key AS sender,lookup.value AS sender_lookup
                FROM request_targets AS target,json_each(:senders) AS sender,
                    json_each(sender.value) AS lookup
                UNION ALL
                SELECT * FROM unrecorded WHERE sender_lookup IS NOT NULL
            ), roots AS MATERIALIZED (
                SELECT row.target,row.sender,row.sender_lookup,row.time_root AS root,row.seq AS latest_seq
                FROM route_keys AS route
                JOIN "{RouteEntryRow.declared_name}" AS row ON row.seq=(
                    SELECT latest.seq FROM "{RouteEntryRow.declared_name}" AS latest
                    WHERE latest.target=route.target AND latest.sender=route.sender
                        AND latest.sender_lookup=route.sender_lookup AND latest.seq<=:through
                    ORDER BY latest.seq DESC LIMIT 1)
            ), candidates AS MATERIALIZED (
                SELECT owner.key AS actor,roots.*,
                    delivery_since(
                        owner.key,roots.sender,roots.target,roots.sender_lookup
                    ) AS since
                FROM json_each(:targets) AS owner,json_each(owner.value) AS target
                JOIN roots ON roots.target=target.value
            ), requests AS MATERIALIZED (
                SELECT * FROM candidates WHERE since IS NOT NULL
            )"""

    def _request_bindings(self, actors):
        return {
            "targets": json.dumps({
                actor: sorted(self.deliveries[actor].index_targets) for actor in actors
            }),
            **self.registry.sender_bindings,
            "through": self.source.through,
        }

    def _read_counts(self, seen: Mapping[str, ReadCursor]) -> list[RouteUnread]:
        return RouteUnread.read(self.connection.execute(
                f'''WITH {self._requests}, {ReadCursor.sql(":read_cursors")},
                {ReadCursor.joined_sql}, painted AS (
                    SELECT request.actor,request.root,count(*) AS count
                    FROM cursors AS request,json_each(request.exceptions) AS seen
                    JOIN "{RouteEntryRow.declared_name}" AS row ON row.seq=seen.value
                        AND request.target=row.target AND request.sender=row.sender
                        AND request.sender_lookup=row.sender_lookup AND row.timestamp>=request.since
                    WHERE row.seq<=:through GROUP BY request.actor,request.root
                ) SELECT request.actor,request.target,request.sender,request.since,full.node AS full,
                    prefix.node AS prefix,coalesce(painted.count,0) AS painted
                FROM cursors AS request
                JOIN "{RouteTimeRow.declared_name}" AS full ON full.id=request.root
                LEFT JOIN "{RouteTimeRow.declared_name}" AS prefix ON prefix.id=(
                    SELECT time_root FROM "{RouteEntryRow.declared_name}"
                    WHERE target=request.target AND sender=request.sender AND sender_lookup=request.sender_lookup
                        AND seq<=min(request.high_water,:through)
                    ORDER BY seq DESC LIMIT 1)
                LEFT JOIN painted ON painted.actor=request.actor AND painted.root=request.root
                WHERE request.high_water<request.latest_seq''',
                self._request_bindings(self.deliveries) | ReadCursor.sql_bindings(self.registry,
                    {actor: cursor.counted(self.deliveries[actor], self.registry)
                     for actor, cursor in seen.items()})))

    def _read_deliveries(self, actor: str, seen: ReadCursor, after: int) -> list[UnseenDelivery]:
        return UnseenDelivery.read(self.connection.execute(
                f'''WITH {self._requests}, {ReadCursor.sql(":read_cursors")},
                {ReadCursor.joined_sql} SELECT row.seq,row.target
                FROM cursors AS request JOIN "{RouteEntryRow.declared_name}" AS row
                    ON row.target=request.target AND row.sender=request.sender
                    AND row.sender_lookup=request.sender_lookup
                WHERE request.actor=:actor AND row.timestamp>=request.since
                    AND row.seq>:after AND row.seq<=:through
                    AND {ReadCursor.unread_sql} ORDER BY row.seq''',
                self._request_bindings((actor,)) | {"actor":actor,"after":after}
                | ReadCursor.sql_bindings(self.registry,
                    {actor: seen.counted(self.deliveries[actor], self.registry)})))


class CompletingRouteCounts(OpenedRouteCounts):
    """A reader that may wait completes the index one short-locked unit at a time."""

    def _prepare(self) -> None:
        self._preparation = self.sync(self.source, self._decode)
        while not self._preparation.complete:
            self._preparation = self.sync(self.source, self._decode)
