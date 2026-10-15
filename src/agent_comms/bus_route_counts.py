"""Disposable, declaration-owned route counts over the canonical bus.

The bus owns timestamps and publication identities. This index stores their
projections, advances across complete appended rows, and is reset at migration.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, ClassVar

from .coordination_database import CoordinationStore
from .coordination_errors import CoordinationReadUnavailable
from .errors import RelationViolationError
from .private_path import FileIdentity, FileRevision
from .routing import DeliveryMessage, DeliveryScope
from .store_files import _store_lock
from .typed_table import Column, Index, TypedRow, TypedTable

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot
    from .wire_log import OpenedWireSnapshot


class RouteTable:
    """Disposable tables owned and reset together by the route projection."""


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
    indexes: ClassVar = (Index(("target", "sender", "sender_lookup", "timestamp", "seq")),)


@dataclass(frozen=True)
class RouteTotalRow(RouteTable, TypedTable):
    target: str = field(metadata={"sql": Column(primary_key=True)})
    sender: str = field(metadata={"sql": Column(primary_key=True)})
    sender_lookup: str = field(metadata={"sql": Column(primary_key=True)})
    total: int
    oldest: float
    without_rowid: ClassVar = True


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


@dataclass(frozen=True)
class RouteIndexReady(RouteIndexState):
    complete: ClassVar[bool] = True


@dataclass(frozen=True)
class RouteIndexBehind(RouteIndexState):
    complete: ClassVar[bool] = False
    through: int
    size: int


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
        self.path = bus_path.with_name("bus_route_counts.sqlite3")

    def __enter__(self) -> BusRouteCounts:
        """Open one committed WAL snapshot without taking the writer lock.

        A WAL reader still meets SQLite's own short busy windows (checkpoint,
        WAL reset, recovery after the last connection closed); it waits them
        out through SQLite's busy handler instead of failing the read.
        """
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
                table = current.execute(
                    f"SELECT 1 FROM sqlite_master WHERE type='table' "
                    f"AND name='{RouteSourceRow.declared_name}'"
                ).fetchone()
                if table and current.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                    return
        with _store_lock(self.path), closing(CoordinationStore.writer(self.path)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute(
                f"SELECT 1 FROM sqlite_master WHERE type='table' "
                f"AND name='{RouteSourceRow.declared_name}'"
            ).fetchone():
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
            # The source owns read custody; the row owns projection continuity.
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
        rebuild = recorded is None or source is None
        offset = recorded.revision.size if recorded and recorded.revision else 0
        if not rebuild and offset:
            if not recorded.matches_file(source):
                rebuild, offset = True, 0
            elif size < offset:
                return RouteIndexReady()
        if not rebuild and offset == size:
            return RouteIndexReady()

        rows: list[RouteEntryRow] = []
        totals: dict[tuple[str, str, str], tuple[int, float]] = {}
        through = size
        if source is not None:
            through = min(size, offset + self.rebuild_bytes)
            if through < size:
                source.seek(through)
                through += len(source.readline(size - through))
            for delivery in self._decoded(source, offset, through, decode):
                message = delivery.message
                row = RouteEntryRow(
                    message.seq, message.target, message.sender,
                    delivery.sender_lookup, message.timestamp,
                )
                rows.append(row)
                key = row.target, row.sender, row.sender_lookup
                count, oldest = totals.get(key, (0, row.timestamp))
                totals[key] = count + 1, min(oldest, row.timestamp)
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
                    row.insert(connection)
                for (target, sender, sender_lookup), (count, oldest) in totals.items():
                    connection.execute(
                        f'INSERT INTO "{RouteTotalRow.declared_name}" '
                        '(target,sender,sender_lookup,total,oldest) VALUES (?,?,?,?,?) '
                        'ON CONFLICT(target,sender,sender_lookup) DO UPDATE SET '
                        'total=total+excluded.total, oldest=min(oldest,excluded.oldest)',
                        (target, sender, sender_lookup, count, oldest),
                    )
                connection.execute(f'DELETE FROM "{RouteSourceRow.declared_name}"')
                RouteSourceRow(1, checkpoint, tail_digest).insert(connection)
                connection.commit()
        except BlockingIOError:
            return RouteIndexBehind(offset, size)
        return RouteIndexReady() if through == size else RouteIndexBehind(through, size)


class OpenedRouteCounts(BusRouteCounts):
    """Delivery projection bound to one original wire cut and namespace.

    The index may lag this cut or already cover later rows. Its stored totals
    are not the reader's source authority. Both queries and the unindexed
    remainder belong to this reader, never to an independently supplied cap.
    """

    def __init__(
        self, bus_path: Path, source: OpenedWireSnapshot,
        registry: RegistrySnapshot, deliveries: Mapping[str, DeliveryScope],
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

    def __enter__(self) -> OpenedRouteCounts:
        self.sync(self.source.stream, self.source.revision, self._decode, blocking=False)
        super().__enter__()
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
            self._targets = json.dumps({
                actor: sorted(delivery.index_targets)
                for actor, delivery in self.deliveries.items()
            })
        except BaseException:
            self.connection.close()
            raise
        return self

    def _appended_deliveries(self) -> tuple[DeliveryMessage, ...]:
        """The bounded part of this cut still missing from the SQLite snapshot."""
        stream = self.source.stream
        size = self.source.boundary
        records = RouteSourceRow.select(self.connection)
        recorded = records[0] if records else None
        through = recorded.revision.size if recorded and recorded.revision else 0
        if stream is None:
            if through:
                raise RouteIndexRebuilding(0, size)
            return ()
        if recorded is not None and not recorded.matches_file(stream):
            raise RouteIndexRebuilding(0, size)
        if size - through > self.rebuild_bytes:
            raise RouteIndexRebuilding(through, size)
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

    _requests = f"""candidates AS MATERIALIZED (
                SELECT owner.key AS actor, route.target, route.sender, route.sender_lookup,
                    delivery_since(
                        owner.key, route.sender, route.target, route.sender_lookup
                    ) AS since
                FROM json_each(:targets) AS owner, json_each(owner.value) AS target
                JOIN "{RouteTotalRow.declared_name}" AS route ON route.target=target.value
            ), requests AS (SELECT * FROM candidates WHERE since IS NOT NULL)"""

    def _read_counts(self, seen: Mapping[str, frozenset[int]]) -> list[RouteUnread]:
        """Exact cut totals: indexed totals minus only their newer suffix.

        The ordinary case reads no historical route rows. Birth crossings use
        the covering timestamp index; sparse painted membership uses primary
        keys. A later index contributes only its small out-of-cut suffix, not
        a new scan of the original prefix or a regression of the projection.
        """
        return RouteUnread.read(
            self.connection.execute(
                f'''WITH {self._requests}, actors AS MATERIALIZED (
                SELECT key AS actor, value AS sequences FROM json_each(:seen)
            ), later AS MATERIALIZED (
                SELECT target, sender, sender_lookup, count(*) AS count
                FROM "{RouteEntryRow.declared_name}" NOT INDEXED WHERE seq > :through
                GROUP BY target, sender, sender_lookup
            ), painted AS (
                SELECT request.actor, request.target, request.sender,
                       request.sender_lookup, count(*) AS count
                FROM actors AS owner, json_each(owner.sequences) AS seen
                CROSS JOIN "{RouteEntryRow.declared_name}" AS row ON row.seq = seen.value
                JOIN requests AS request
                    ON request.actor=owner.actor
                    AND request.target=row.target AND request.sender=row.sender
                    AND request.sender_lookup=row.sender_lookup AND row.timestamp>=request.since
                WHERE row.seq <= :through
                GROUP BY request.actor, request.target, request.sender, request.sender_lookup
            )
            SELECT request.actor, request.target, request.sender,
                (CASE WHEN totals.oldest >= request.since
                    THEN totals.total - coalesce(later.count, 0) ELSE (
                    SELECT count(*) FROM "{RouteEntryRow.declared_name}" AS row
                    WHERE row.target=request.target AND row.sender=request.sender
                        AND row.sender_lookup=request.sender_lookup AND row.timestamp>=request.since
                        AND row.seq <= :through
                ) END) - coalesce(painted.count, 0) AS count
            FROM requests AS request JOIN "{RouteTotalRow.declared_name}" AS totals
                ON totals.target=request.target AND totals.sender=request.sender
                    AND totals.sender_lookup=request.sender_lookup
            LEFT JOIN later ON later.target=request.target AND later.sender=request.sender
                AND later.sender_lookup=request.sender_lookup
            LEFT JOIN painted ON painted.actor=request.actor
                AND painted.target=request.target AND painted.sender=request.sender
                AND painted.sender_lookup=request.sender_lookup''',
                {"targets": self._targets,
                 "seen": json.dumps({actor: sorted(rows) for actor, rows in seen.items()}),
                 "through": self.source.through},
            )
        )

    def _read_deliveries(self, actor: str, seen: frozenset[int]) -> list[UnseenDelivery]:
        return UnseenDelivery.read(
            self.connection.execute(
                f'''WITH {self._requests}
            SELECT row.seq, row.target
            FROM requests AS request JOIN "{RouteEntryRow.declared_name}" AS row
                ON row.target=request.target AND row.sender=request.sender
                    AND row.sender_lookup=request.sender_lookup AND row.timestamp>=request.since
            WHERE request.actor=:actor AND row.seq <= :through
                AND row.seq NOT IN (SELECT value FROM json_each(:seen))
            ORDER BY row.seq''',
                {"targets": self._targets,
                 "actor": actor, "seen": json.dumps(sorted(seen)), "through": self.source.through},
            )
        )
