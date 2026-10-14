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
from typing import BinaryIO, ClassVar

from .private_path import FileRevision
from .coordination_errors import CoordinationReadUnavailable
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .store_files import _store_lock
from .routing import DeliveryMessage
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


class RouteIndexState:
    def complete(self) -> bool:
        raise NotImplementedError


class RouteIndexReady(RouteIndexState):
    def complete(self) -> bool:
        return True


class RouteIndexRebuilding(CoordinationReadUnavailable, RouteIndexState):
    """The writer has not committed route counts through the requested cut."""

    def complete(self) -> bool:
        return False

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
        """Open one committed WAL snapshot without taking the writer lock."""
        if not self.path.exists():
            raise RouteIndexRebuilding(0, self.bus_path.stat().st_size if self.bus_path.exists() else 0)
        self.connection = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0
        )
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.execute("BEGIN")
        if not self.connection.execute(
            f"SELECT 1 FROM sqlite_master WHERE type='table' "
            f"AND name='{RouteSourceRow.declared_name}'"
        ).fetchone():
            self.connection.close()
            raise RouteIndexRebuilding(
                0, self.bus_path.stat().st_size if self.bus_path.exists() else 0
            )
        return self

    def __exit__(self, _error_type, _error, _traceback) -> None:
        self.connection.close()

    @staticmethod
    def _tail(source: BinaryIO, offset: int) -> str:
        start = max(0, offset - 4096)
        source.seek(start)
        return sha256(source.read(offset - start)).hexdigest()

    def require_cut(self, source: BinaryIO | None, revision: FileRevision | None) -> None:
        records = RouteSourceRow.select(self.connection)
        recorded = records[0] if records else None
        size = revision.size if revision is not None else 0
        through = recorded.revision.size if recorded and recorded.revision else 0
        if recorded is None or through < size:
            raise RouteIndexRebuilding(through, size)
        if source is not None and through and self._tail(source, through) != recorded.tail_digest:
            raise RouteIndexRebuilding(0, size)

    def _initialize(self) -> None:
        """The writer alone creates the WAL store; no source bytes are decoded here."""
        if self.path.exists():
            with closing(sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0
            )) as current:
                table = current.execute(
                    f"SELECT 1 FROM sqlite_master WHERE type='table' "
                    f"AND name='{RouteSourceRow.declared_name}'"
                ).fetchone()
                if table and current.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                    return
        with _store_lock(self.path), closing(sqlite3.connect(self.path, timeout=30)) as connection:
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
        with self:
            rows = RouteSourceRow.select(self.connection)
            return rows[0] if rows else None

    def sync(
        self, source: BinaryIO | None, revision: FileRevision | None,
        decode: Callable[[Mapping], tuple[DeliveryMessage, ...]],
    ) -> RouteIndexState:
        """Decode a bounded source delta first, then commit under a short lock."""
        size = revision.size if revision is not None else 0
        if source is not None:
            opened = FileRevision.from_stat(os.fstat(source.fileno()))
            if not revision.holds_cut(opened, size):
                raise RelationViolationError("Route projection source changed before its captured read")
            if size:
                source.seek(size - 1)
                if source.read(1) != b"\n":
                    raise RelationViolationError("Route projection source has an incomplete original row")
        self._initialize()
        recorded = self._recorded()
        rebuild = recorded is None or source is None
        offset = recorded.revision.size if recorded and recorded.revision else 0
        if not rebuild and offset:
            if source is None or self._tail(source, offset) != recorded.tail_digest:
                rebuild, offset = True, 0
            elif size < offset:
                return RouteIndexReady()
        if not rebuild and offset == size:
            return RouteIndexReady()

        rows: list[RouteEntryRow] = []
        totals: dict[tuple[str, str, str], tuple[int, float]] = {}
        through = size
        if source is not None:
            source.seek(offset)
            limit = min(size, offset + self.rebuild_bytes)
            while source.tell() < size and source.tell() < limit:
                raw = source.readline(size - source.tell())
                if not raw.endswith(b"\n"):
                    raise RelationViolationError("Route projection source has an incomplete original row")
                if not raw.strip():
                    continue
                for delivery in decode(json.loads(raw)):
                    message = delivery.message
                    row = RouteEntryRow(
                        message.seq, message.target, message.sender,
                        delivery.sender_lookup, message.timestamp,
                    )
                    rows.append(row)
                    key = row.target, row.sender, row.sender_lookup
                    count, oldest = totals.get(key, (0, row.timestamp))
                    totals[key] = count + 1, min(oldest, row.timestamp)
            through = source.tell()
        tail_digest = self._tail(source, through) if source is not None else None
        checkpoint = replace(revision, size=through) if revision is not None else None

        with _store_lock(self.path), closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            current_rows = RouteSourceRow.select(connection)
            current = current_rows[0] if current_rows else None
            if current != recorded:
                connection.rollback()
                current_through = current.revision.size if current and current.revision else 0
                return (RouteIndexReady() if current_through >= size
                        else RouteIndexRebuilding(current_through, size))
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
        return RouteIndexReady() if through == size else RouteIndexRebuilding(through, size)

    _requests = """requests AS (
                SELECT json_extract(value, '$.actor') AS actor,
                       json_extract(value, '$.target') AS target,
                       json_extract(value, '$.sender') AS sender,
                       json_extract(value, '$.sender_lookup') AS sender_lookup,
                       json_extract(value, '$.since') AS since FROM json_each(?)
            )"""

    def routes(self) -> list[RouteTotalRow]:
        return RouteTotalRow.select(self.connection)

    def unseen_counts(
        self, requests: list[PendingRoute], seen: list[ActorSeen]
    ) -> list[RouteUnread]:
        """One bulk query: route totals minus exact seen membership.

        Common routes use prefix totals, without walking historical rows. A
        birth crossing a route uses its covering timestamp index; timestamps
        need not be monotonic. Seen sequences use the primary-key index once
        per actor, not once per historical message or conversation.
        """
        return RouteUnread.read(
            self.connection.execute(
                f'''WITH {self._requests}, painted AS (
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

    def unseen_deliveries(
        self, requests: list[PendingRoute], seen: frozenset[int]
    ) -> list[UnseenDelivery]:
        """One actor's unread deliveries on its routes, oldest first, without bodies.

        The same route rows as ``unseen_counts``, listed instead of counted.
        """
        return UnseenDelivery.read(
            self.connection.execute(
                f'''WITH {self._requests}
            SELECT row.seq, row.target
            FROM requests AS request JOIN "{RouteEntryRow.declared_name}" AS row
                ON row.target=request.target AND row.sender=request.sender
                    AND row.sender_lookup=request.sender_lookup AND row.timestamp>=request.since
            WHERE row.seq NOT IN (SELECT value FROM json_each(?))
            ORDER BY row.seq''',
                (json.dumps(FieldCodec.encode(requests)), json.dumps(sorted(seen))),
            )
        )
