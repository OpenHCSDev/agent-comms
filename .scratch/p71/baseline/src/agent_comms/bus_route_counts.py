"""Disposable, declaration-owned route counts over the canonical bus.

The bus owns timestamps and publication identities. This index stores their
projections, advances across complete appended rows, and is reset at migration.
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
from abc import abstractmethod
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, ClassVar, NoReturn

from .coordination_database import CoordinationStore
from .coordination_errors import CoordinationReadUnavailable
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import projected
from .private_path import FileIdentity, FileRevision
from .routing import DeliveryMessage, DeliveryScope
from .store_files import _store_lock
from .typed_table import Column, ForeignKey, Index, SQLiteJournalMode, TypedRow, TypedTable

if TYPE_CHECKING:
    from .registry_provenance import RegistryProvenance
    from .wire_log import OpenedWireSnapshot


class RouteTable:
    """Disposable tables owned and reset together by the route projection."""

    schema: ClassVar[int] = 3


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

    @property
    @abstractmethod
    def heads(self) -> tuple[int, ...]: ...

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

    @projected(view="wire")
    def minimum(self):
        return self.timestamp

    @projected(view="wire")
    def maximum(self):
        return self.timestamp

    @projected(view="wire")
    def children(self):
        return ()

    @projected(view="wire")
    def heads(self):
        return (self.head_seq,)

    def grow(self, db, leaf):
        return RouteTimeRow.store(db, replace(leaf, count=self.count + 1)), self.head_seq


@dataclass(frozen=True, kw_only=True)
class ForkRouteTimeNode(RouteTimeNode):
    lower: float
    upper: float
    left: int
    right: int

    @projected(view="wire")
    def minimum(self):
        return self.lower

    @projected(view="wire")
    def maximum(self):
        return self.upper

    @projected(view="wire")
    def children(self):
        return (self.left, self.right)

    @projected(view="wire")
    def heads(self):
        return ()

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


@dataclass(frozen=True)
class RouteSourceRow(RouteTable, TypedTable):
    id: int = field(metadata={"sql": Column(primary_key=True)})
    revision: FileRevision | None
    tail_digest: str | None

    @staticmethod
    def tail(source: BinaryIO, offset: int) -> str:
        start = max(0, offset - 4096)
        source.seek(start)
        return sha256(source.read(offset - start)).hexdigest()

    def matches_file(self, source: BinaryIO) -> bool:
        if self.revision is None:
            return True  # The empty projection has no original bytes to retain.
        return (
            self.revision.identity == FileIdentity.from_stat(os.fstat(source.fileno()))
            and self.tail(source, self.revision.size) == self.tail_digest
        )


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
    count: int


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
        try:
            return self._acquire()
        except sqlite3.OperationalError as error:
            CoordinationReadUnavailable.raise_from(error)

    def _acquire(self) -> BusRouteCounts:
        self.connection = self._open()
        return self

    def _open(self) -> sqlite3.Connection:
        self._initialize()
        return CoordinationStore.read_snapshot(self.path)

    def __exit__(self, _error_type, _error, _traceback) -> None:
        self.connection.close()

    @staticmethod
    def _decoded(
        source: BinaryIO, start: int, stop: int,
        decode: Callable[[Mapping], tuple[DeliveryMessage, ...]],
    ):
        """Deliveries of the complete original rows in [start, stop)."""
        source.seek(start)
        while source.tell() < stop:
            raw = source.readline(stop - source.tell())
            if not raw.endswith(b"\n"):
                raise RelationViolationError(
                    "Route projection source has an incomplete original row"
                )
            if raw.strip():
                yield from decode(json.loads(raw))

    def _initialize(self) -> None:
        """The writer alone creates the WAL store; no source bytes are decoded here."""
        if self.path.exists():
            with closing(CoordinationStore.read_snapshot(self.path)) as current:
                if (RouteSourceRow.present(current)
                        and SQLiteJournalMode.read(current.execute("PRAGMA journal_mode"))[0].journal_mode == "wal"):
                    return
        with _store_lock(self.path), closing(CoordinationStore.writer(self.path)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            if not RouteSourceRow.present(connection):
                for owner in TypedTable.members_with(RouteTable):
                    owner.create(connection)
            connection.commit()

    def prepare(self) -> None:
        """Create the writer-owned WAL store after its publication lock releases."""
        self._initialize()

    def _recorded(self) -> RouteSourceRow | None:
        with closing(self._open()) as connection:
            rows = RouteSourceRow.select(connection)
            return rows[0] if rows else None

    def sync(
        self, source: BinaryIO | None, revision: FileRevision | None,
        decode: Callable[[Mapping], tuple[DeliveryMessage, ...]],
        *, blocking: bool = True,
    ) -> RouteIndexState:
        """Decode a bounded source delta first, then commit under a short lock.

        A reader passes ``blocking=False``: when another writer holds the
        projection lock it is committing the same bytes, so this reader
        answers from its own remainder instead of waiting.
        """
        size = revision.size if revision is not None else 0
        if source is not None:
            # Touching or unlinking a retained inode does not change its cut.
            # Read the captured open file; the stored row checks index continuity.
            if revision.identity != FileIdentity.from_stat(os.fstat(source.fileno())):
                raise RelationViolationError(
                    "Route projection source changed before its captured read"
                )
            if size:
                source.seek(size - 1)
                if source.read(1) != b"\n":
                    raise RelationViolationError(
                        "Route projection source has an incomplete original row"
                    )
        recorded = self._recorded()
        if size == 0 and recorded is not None:
            # An original empty cut cannot retire a later committed projection.
            return RouteIndexReady()
        rebuild = recorded is None or source is None
        offset = recorded.revision.size if recorded and recorded.revision else 0
        if not rebuild and offset:
            if not recorded.matches_file(source):
                rebuild, offset = True, 0
            elif size < offset:
                return RouteIndexReady()
        if not rebuild and offset == size:
            return RouteIndexReady()

        rows: list[DeliveryMessage] = []
        through = size
        if source is not None:
            through = min(size, offset + self.rebuild_bytes)
            if through < size:
                source.seek(through)
                through += len(source.readline(size - through))
            rows.extend(self._decoded(source, offset, through, decode))
        tail_digest = RouteSourceRow.tail(source, through) if source is not None else None
        checkpoint = replace(revision, size=through) if revision is not None else None

        try:
            with (
                _store_lock(self.path, blocking=blocking),
                closing(CoordinationStore.writer(self.path)) as connection,
            ):
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                current_rows = RouteSourceRow.select(connection)
                current = current_rows[0] if current_rows else None
                if current != recorded:
                    connection.rollback()
                    current_through = current.revision.size if current and current.revision else 0
                    return (RouteIndexReady() if current_through >= size
                            else RouteIndexBehind(current_through, size))
                if rebuild:
                    for owner in TypedTable.members_with(RouteTable):
                        connection.execute(f'DELETE FROM "{owner.declared_name}"')
                for row in rows:
                    RouteEntryRow.record(connection, row)
                connection.execute(f'DELETE FROM "{RouteSourceRow.declared_name}"')
                RouteSourceRow(1, checkpoint, tail_digest).insert(connection)
                connection.commit()
        except BlockingIOError:
            return RouteIndexBlocked()
        return RouteIndexReady() if through == size else RouteIndexBehind(through, size)


class OpenedRouteCounts(BusRouteCounts):
    """Delivery projection bound to one original wire cut and namespace.

    The index may lag this cut or already cover later rows. Its stored totals
    are not the reader's source authority. Both queries and the unindexed
    remainder belong to this reader, never to an independently supplied cap.
    """

    def __init__(
        self, bus_path: Path, source: OpenedWireSnapshot,
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

    def _acquire(self) -> OpenedRouteCounts:
        self._preparation = self.sync(
            self.source.stream, self.source.revision, self._decode, blocking=False
        )
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
        stream = self.source.stream
        size = self.source.boundary
        if size == 0:
            return ()
        records = RouteSourceRow.select(self.connection)
        recorded = records[0] if records else None
        through = recorded.revision.size if recorded and recorded.revision else 0
        if stream is None:
            if through:
                self._preparation.defer_read(0, size)
            return ()
        if recorded is not None and not recorded.matches_file(stream):
            self._preparation.defer_read(0, size)
        if size - through > self.rebuild_bytes:
            self._preparation.defer_read(through, size)
        if through >= size:
            return ()
        return tuple(self._decoded(stream, through, size, self._decode))

    def _recipients(self, target: str):
        direct = self.registry.canonical_name(target)
        return self._channel_members.get(
            target, {direct} if direct in self.deliveries else set()
        )

    def unseen_counts(self, seen: Mapping[str, frozenset[int]]) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {actor: {} for actor in self.deliveries}

        def add(actor: str, sender: str, target: str, count: int):
            if count:
                conversation = self.deliveries[actor].conversation(sender, target)
                counts[actor][conversation] = counts[actor].get(conversation, 0) + count

        for row in self._read_counts(seen):
            add(row.actor, row.sender, row.target, row.count)
        for item in self._appended:
            message = item.message
            for actor in self._recipients(message.target):
                if self.deliveries[actor].unread(item, self.registry, seen[actor]):
                    add(actor, message.sender, message.target, 1)
        return counts

    def unseen_deliveries(self, actor: str, seen: frozenset[int]) -> tuple[UnseenDelivery, ...]:
        delivery = self.deliveries[actor]
        return (*self._read_deliveries(actor, seen), *(
            UnseenDelivery(item.message.seq, item.message.target)
            for item in self._appended
            if delivery.unread(item, self.registry, seen)
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
                SELECT row.target,row.sender,row.sender_lookup,row.time_root AS root
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
        senders = {}
        unrecorded = []
        for name, source in self.registry.named_members():
            senders[name] = tuple(DeliveryScope.sender_lookups(
                source.incarnation, lambda: unrecorded.append(name)
            ))
        return {
            "targets": json.dumps({
                actor: sorted(self.deliveries[actor].index_targets) for actor in actors
            }),
            "senders": json.dumps(senders), "unrecorded": json.dumps(unrecorded),
            "through": self.source.through,
        }

    # One eligibility implementation supplies both counts and listings. The
    # accepted frontier has disjoint subtrees; count uses their stored ranks,
    # while listing alone descends to the original delivery sequences.
    _eligible = f"""timestamp_walk(actor,root,since,id,node) AS (
                SELECT request.actor, request.root, request.since, tree.id, tree.node
                FROM requests AS request JOIN "{RouteTimeRow.declared_name}" AS tree
                    ON tree.id=request.root
                UNION ALL
                SELECT walk.actor, walk.root, walk.since, child.id, child.node
                FROM timestamp_walk AS walk, json_each(walk.node,'$.children') AS edge
                JOIN "{RouteTimeRow.declared_name}" AS child ON child.id=edge.value
                WHERE json_extract(walk.node,'$.minimum')<walk.since
                    AND json_extract(walk.node,'$.maximum')>=walk.since
            ), eligible AS MATERIALIZED (
                SELECT actor,root,id,node FROM timestamp_walk
                WHERE json_extract(node,'$.minimum')>=since
            )"""

    def _read_counts(self, seen: Mapping[str, frozenset[int]]) -> list[RouteUnread]:
        """Seek each original route root, rank timestamps, subtract sparse paint.

        Threshold traversal visits at most twice the finite key width plus one
        node per route, even for nonmonotone timestamps. No later suffix or
        original historical message range is scanned.
        """
        return RouteUnread.read(self.connection.execute(
            f'''WITH RECURSIVE {self._requests}, {self._eligible},
            totals AS (
                SELECT actor,root,sum(json_extract(node,'$.count')) AS count
                FROM eligible GROUP BY actor,root
            ), actors AS MATERIALIZED (
                SELECT key AS actor, value AS sequences FROM json_each(:seen)
            ), painted AS (
                SELECT request.actor, request.root, count(*) AS count
                FROM actors AS owner, json_each(owner.sequences) AS seen
                CROSS JOIN "{RouteEntryRow.declared_name}" AS row ON row.seq=seen.value
                JOIN requests AS request
                    ON request.actor=owner.actor
                    AND request.target=row.target AND request.sender=row.sender
                    AND request.sender_lookup=row.sender_lookup AND row.timestamp>=request.since
                WHERE row.seq<=:through GROUP BY request.actor,request.root
            )
            SELECT request.actor,request.target,request.sender,
                coalesce(totals.count,0)-coalesce(painted.count,0) AS count
            FROM requests AS request
            LEFT JOIN totals ON totals.actor=request.actor AND totals.root=request.root
            LEFT JOIN painted ON painted.actor=request.actor AND painted.root=request.root''',
            self._request_bindings(self.deliveries) | {
                "seen": json.dumps({actor: sorted(rows) for actor, rows in seen.items()}),
            },
        ))

    def _read_deliveries(self, actor: str, seen: frozenset[int]) -> list[UnseenDelivery]:
        """Visit eligible original sequences, never a future timestamp range."""
        return UnseenDelivery.read(self.connection.execute(
            f'''WITH RECURSIVE {self._requests}, {self._eligible},
            listing_nodes(id,node) AS (
                SELECT id,node FROM eligible WHERE actor=:actor
                UNION ALL
                SELECT child.id,child.node
                FROM listing_nodes AS parent, json_each(parent.node,'$.children') AS edge
                JOIN "{RouteTimeRow.declared_name}" AS child ON child.id=edge.value
            ), sequences(seq) AS (
                SELECT head.value FROM listing_nodes AS leaf,
                    json_each(leaf.node,'$.heads') AS head
                UNION ALL
                SELECT row.previous_at_timestamp FROM sequences AS chain
                JOIN "{RouteEntryRow.declared_name}" AS row ON row.seq=chain.seq
                WHERE row.previous_at_timestamp IS NOT NULL
            )
            SELECT row.seq,row.target FROM sequences AS listed
            JOIN "{RouteEntryRow.declared_name}" AS row ON row.seq=listed.seq
            WHERE row.seq NOT IN (SELECT value FROM json_each(:seen)) ORDER BY row.seq''',
            self._request_bindings((actor,)) | {"actor": actor, "seen": json.dumps(sorted(seen))},
        ))
