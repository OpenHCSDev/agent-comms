"""Disposable route counts advanced by the append owner and read from committed WAL."""

from __future__ import annotations

import json
import os
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO, ClassVar, TypeVar

from .bus_projection import (
    AppendCheckpoint,
    AppendSegment,
    BusAppendIndex,
    ProjectionCoverage,
    ProjectionRead,
    SQLiteProjection,
)
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .private_path import FileRevision
from .routing import DeliveryMessage
from .store_files import _store_lock
from .typed_table import Column, Index, TypedRow, TypedTable


class RouteTable:
    """Disposable tables owned and reset together by the route projection."""


@dataclass(frozen=True)
class RouteSourceRow(RouteTable, TypedTable):
    id: int = field(metadata={"sql": Column(primary_key=True)})
    revision: FileRevision | None
    tail_digest: str | None


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
class PendingRoute:
    actor: str
    target: str
    sender: str
    sender_lookup: str
    since: float


@dataclass(frozen=True)
class ActorSeen:
    actor: str
    sequences: frozenset[int]


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


class RouteCheckpoint(ABC):
    """Typed captured checkpoint state used by replay and compare-before-commit."""

    @property
    @abstractmethod
    def through(self) -> int:
        """Committed source byte boundary."""

    @abstractmethod
    def validate(self, source: BinaryIO, size: int) -> RouteCheckpoint:
        """Return this checkpoint or the initial state from tail evidence alone."""

    @abstractmethod
    def committed(self, connection: sqlite3.Connection, query, empty):
        """Return rows supported by this checkpoint state."""

    @abstractmethod
    def reset(self, connection: sqlite3.Connection) -> None:
        """Prepare tables for committing rows derived after this state."""


@dataclass(frozen=True)
class InitialRouteCheckpoint(RouteCheckpoint):
    @property
    def through(self) -> int:
        return 0

    def validate(self, source: BinaryIO, size: int) -> RouteCheckpoint:
        return self

    def committed(self, connection: sqlite3.Connection, query, empty):
        del connection, query
        return empty

    def reset(self, connection: sqlite3.Connection) -> None:
        for owner in TypedTable.members_with(RouteTable):
            connection.execute(f'DELETE FROM "{owner.declared_name}"')


@dataclass(frozen=True)
class StoredRouteCheckpoint(RouteCheckpoint):
    row: RouteSourceRow

    @property
    def through(self) -> int:
        revision = self.row.revision
        return revision.size if revision is not None else 0

    def validate(self, source: BinaryIO, size: int) -> RouteCheckpoint:
        del size
        through = self.through
        if through == 0 and self.row.tail_digest is None:
            return self
        if self.row.tail_digest == AppendCheckpoint.fingerprint(source, through):
            return self
        return InitialRouteCheckpoint()

    def committed(self, connection: sqlite3.Connection, query, empty):
        del empty
        return query(connection)

    def reset(self, connection: sqlite3.Connection) -> None:
        del connection


@dataclass(frozen=True)
class RouteReplay:
    expected: RouteCheckpoint
    base: RouteCheckpoint
    segment: AppendSegment
    revision: FileRevision | None
    rows: tuple[RouteEntryRow, ...]
    totals: tuple[tuple[tuple[str, str, str], int, float], ...]


ReadValue = TypeVar("ReadValue")


class BusRouteCounts(SQLiteProjection, BusAppendIndex[AppendCheckpoint]):
    """Route projection writer plus nonblocking committed WAL reader."""

    replay_bytes = 4 * 1024 * 1024

    def __init__(self, bus_path: Path):
        BusAppendIndex.__init__(self, bus_path, bus_path.with_name("bus_route_counts.sqlite3"))

    @property
    def sqlite_tables(self) -> tuple[type, ...]:
        return TypedTable.members_with(RouteTable)

    def initialize_projection(self, connection: sqlite3.Connection) -> None:
        # Touch owned table pages during one-time WAL creation; no sentinel row persists.
        for table in self.sqlite_tables:
            connection.execute(f'DELETE FROM "{table.declared_name}"')

    @staticmethod
    def _checkpoint(connection: sqlite3.Connection) -> RouteCheckpoint:
        rows = RouteSourceRow.select(connection)
        if rows and rows[0].revision is not None:
            return StoredRouteCheckpoint(rows[0])
        return InitialRouteCheckpoint()

    def prepare(self) -> None:
        """The append owner creates the WAL before scheduling projection replay."""
        self.initialize_sqlite()

    def read(
        self,
        source: BinaryIO | None,
        revision: FileRevision | None,
        query: Callable[[sqlite3.Connection], ReadValue],
        empty: ReadValue,
    ) -> ProjectionRead[ReadValue]:
        """Read committed rows immediately and report their typed source coverage."""
        size = revision.size if revision is not None else 0
        if not self.path.exists():
            return ProjectionRead(self.coverage(0, size), empty)
        try:
            with self.sqlite_reader() as connection:
                checkpoint = self._checkpoint(connection)
                # Validation hashes the recorded tail even when this borrowed
                # cut is older than the projection. Append preserves it;
                # replacement or truncation does not.
                if source is not None:
                    checkpoint = checkpoint.validate(source, size)
                committed = checkpoint.committed(connection, query, empty)
        except sqlite3.DatabaseError:
            return ProjectionRead(self.coverage(0, size), empty)
        return ProjectionRead(self.coverage(checkpoint.through, size), committed)

    @staticmethod
    def replay(
        expected: RouteCheckpoint,
        base: RouteCheckpoint,
        segment: AppendSegment,
        revision: FileRevision | None,
        decode: Callable[[Mapping], tuple[DeliveryMessage, ...]],
    ) -> RouteReplay:
        """Project already-decoded source rows without a projection lock."""
        rows = []
        totals: dict[tuple[str, str, str], tuple[int, float]] = {}
        for record in segment.records:
            for delivery in decode(record):
                message = delivery.message
                row = RouteEntryRow(
                    message.seq, message.target, message.sender,
                    delivery.sender_lookup, message.timestamp,
                )
                rows.append(row)
                key = row.target, row.sender, row.sender_lookup
                count, oldest = totals.get(key, (0, row.timestamp))
                totals[key] = count + 1, min(oldest, row.timestamp)
        return RouteReplay(
            expected,
            base,
            segment,
            replace(revision, size=segment.through) if revision is not None else None,
            tuple(rows),
            tuple((key, count, oldest) for key, (count, oldest) in totals.items()),
        )

    def commit(self, replay: RouteReplay) -> ProjectionCoverage:
        """Verify one constant-size checkpoint, then commit an already-decoded batch."""
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            connection.execute("PRAGMA synchronous=NORMAL")
            with _store_lock(self.path):
                connection.execute("BEGIN IMMEDIATE")
                current = self._checkpoint(connection)
                if current != replay.expected:
                    connection.rollback()
                    return self.coverage(current.through, replay.segment.size)
                replay.base.reset(connection)
                for row in replay.rows:
                    row.insert(connection)
                for (target, sender, sender_lookup), count, oldest in replay.totals:
                    connection.execute(
                        f'INSERT INTO "{RouteTotalRow.declared_name}" '
                        '(target,sender,sender_lookup,total,oldest) VALUES (?,?,?,?,?) '
                        'ON CONFLICT(target,sender,sender_lookup) DO UPDATE SET '
                        'total=total+excluded.total, oldest=min(oldest,excluded.oldest)',
                        (target, sender, sender_lookup, count, oldest),
                    )
                connection.execute(f'DELETE FROM "{RouteSourceRow.declared_name}"')
                RouteSourceRow(1, replay.revision, replay.segment.tail).insert(connection)
                connection.commit()
        return replay.segment.coverage

    def sync(
        self, source: BinaryIO | None, revision: FileRevision | None,
        decode: Callable[[Mapping], tuple[DeliveryMessage, ...]],
    ) -> ProjectionCoverage:
        """Capture and decode a bounded segment, then enter the short commit lock."""
        size = revision.size if revision is not None else 0
        self.initialize_sqlite()
        with self.sqlite_reader() as connection:
            expected = self._checkpoint(connection)
        if source is None:
            return self.coverage(expected.through, size)
        opened = FileRevision.from_stat(os.fstat(source.fileno()))
        if (
            revision is None
            or revision.identity != opened.identity
            or opened.size < size
        ):
            raise RelationViolationError("Route projection source changed before its captured read")
        base = expected.validate(source, size)
        segment = self.capture_segment(
            source, after=base.through, size=size, max_bytes=self.replay_bytes
        )
        return self.commit(self.replay(expected, base, segment, revision, decode))

    _requests = """requests AS (
                SELECT json_extract(value, '$.actor') AS actor,
                       json_extract(value, '$.target') AS target,
                       json_extract(value, '$.sender') AS sender,
                       json_extract(value, '$.sender_lookup') AS sender_lookup,
                       json_extract(value, '$.since') AS since FROM json_each(?)
            )"""

    @staticmethod
    def routes(connection: sqlite3.Connection) -> list[RouteTotalRow]:
        return RouteTotalRow.select(connection)

    @classmethod
    def unseen_counts(
        cls, connection: sqlite3.Connection,
        requests: list[PendingRoute], seen: list[ActorSeen],
    ) -> list[RouteUnread]:
        return RouteUnread.read(
            connection.execute(
                f'''WITH {cls._requests}, painted AS (
                SELECT json_extract(owner.value, '$.actor') AS actor,
                       row.target, row.sender, row.sender_lookup, row.timestamp
                FROM json_each(?) AS owner, json_each(owner.value, '$.sequences') AS seen
                CROSS JOIN "{RouteEntryRow.declared_name}" AS row ON row.seq = seen.value
            )
            SELECT request.actor, request.target, request.sender,
                (CASE WHEN totals.oldest >= request.since THEN totals.total ELSE (
                    SELECT count(*) FROM "{RouteEntryRow.declared_name}" AS row
                    WHERE row.target=request.target AND row.sender=request.sender
                        AND row.sender_lookup=request.sender_lookup AND row.timestamp>=request.since
                ) END) - (
                    SELECT count(*) FROM painted WHERE painted.actor=request.actor
                        AND painted.target=request.target AND painted.sender=request.sender
                        AND painted.sender_lookup=request.sender_lookup
                        AND painted.timestamp>=request.since
                ) AS count
            FROM requests AS request JOIN "{RouteTotalRow.declared_name}" AS totals
                ON totals.target=request.target AND totals.sender=request.sender
                    AND totals.sender_lookup=request.sender_lookup''',
                (json.dumps(FieldCodec.encode(requests)), json.dumps(FieldCodec.encode(seen))),
            )
        )

    @classmethod
    def unseen_deliveries(
        cls, connection: sqlite3.Connection,
        requests: list[PendingRoute], seen: frozenset[int],
    ) -> list[UnseenDelivery]:
        return UnseenDelivery.read(
            connection.execute(
                f'''WITH {cls._requests}
            SELECT row.seq, row.target
            FROM requests AS request JOIN "{RouteEntryRow.declared_name}" AS row
                ON row.target=request.target AND row.sender=request.sender
                    AND row.sender_lookup=request.sender_lookup AND row.timestamp>=request.since
            WHERE row.seq NOT IN (SELECT value FROM json_each(?))
            ORDER BY row.seq''',
                (json.dumps(FieldCodec.encode(requests)), json.dumps(sorted(seen))),
            )
        )
