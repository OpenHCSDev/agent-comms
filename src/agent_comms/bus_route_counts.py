"""Disposable, declaration-owned route counts over the canonical bus.

The bus owns timestamps and publication identities. This index stores their
projections, advances across complete appended rows, and is reset at migration.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import ClassVar

from .field_codec import FieldCodec
from .routing import DeliveryMessage
from .typed_table import Column, Index, TypedRow, TypedTable


class RouteTable:
    """Disposable tables owned and reset together by the route projection."""


@dataclass(frozen=True)
class RouteSourceRow(RouteTable, TypedTable):
    id: int = field(metadata={"sql": Column(primary_key=True)})
    identity: tuple[int, int, int, int] | None
    offset: int
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


class BusRouteCounts:
    def __init__(self, bus_path: Path):
        self.bus_path = bus_path
        self.path = bus_path.with_name("bus_route_counts.sqlite3")
        fresh = not self.path.exists()
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.execute("PRAGMA synchronous=FULL")
        if fresh:
            with self.connection:
                for owner in TypedTable.members_with(RouteTable):
                    owner.create(self.connection)

    def __enter__(self) -> BusRouteCounts:
        return self

    def __exit__(self, *_error: object) -> None:
        self.connection.close()

    def sync(self, decode: Callable[[Mapping], tuple[DeliveryMessage, ...]]) -> bool:
        """Decode only appended rows; a replaced source rebuilds the projection."""
        try:
            source = self.bus_path.open("rb")
        except FileNotFoundError:
            source = None
        try:
            identity, size, tail_digest = None, 0, None
            if source is not None:
                st = os.fstat(source.fileno())
                identity = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)
                size = st.st_size
                if size:
                    source.seek(-1, os.SEEK_END)
                    if source.read(1) != b"\n":
                        return False
                source.seek(max(0, size - 4096))
                tail_digest = sha256(source.read()).hexdigest()
            records = RouteSourceRow.select(self.connection)
            recorded = records[0] if records else None
            rebuild = recorded is None
            if recorded is not None:
                rebuild = (
                    (recorded.identity[:2] if recorded.identity else None)
                    != (identity[:2] if identity else None)
                    or size < recorded.offset
                    or size == recorded.offset
                    and recorded.identity != identity
                )
            offset = 0 if rebuild else recorded.offset
            if source is not None and offset:
                source.seek(max(0, offset - 4096))
                previous_tail = source.read(offset - max(0, offset - 4096))
                if sha256(previous_tail).hexdigest() != recorded.tail_digest:
                    rebuild, offset = True, 0
            if not rebuild and offset == size:
                return True
            totals: dict[tuple[str, str, str], RouteTotalRow] = {}
            with self.connection:
                if rebuild:
                    for owner in TypedTable.members_with(RouteTable):
                        self.connection.execute(f'DELETE FROM "{owner.declared_name}"')
                if source is not None:
                    source.seek(offset)
                    for raw in source:
                        if not raw.strip():
                            continue
                        for delivery in decode(json.loads(raw)):
                            message = delivery.message
                            row = RouteEntryRow(
                                message.seq,
                                message.target,
                                message.sender,
                                delivery.sender_lookup,
                                message.timestamp,
                            )
                            row.insert(self.connection)
                            key = row.target, row.sender, row.sender_lookup
                            if key not in totals:
                                prior = RouteTotalRow.select(
                                    self.connection,
                                    where="target=? AND sender=? AND sender_lookup=?",
                                    parameters=key,
                                )
                                totals[key] = (
                                    prior[0] if prior else RouteTotalRow(*key, 0, row.timestamp)
                                )
                            current = totals[key]
                            totals[key] = replace(
                                current,
                                total=current.total + 1,
                                oldest=min(current.oldest, row.timestamp),
                            )
                for key, total in totals.items():
                    self.connection.execute(
                        f'DELETE FROM "{RouteTotalRow.declared_name}" '
                        "WHERE target=? AND sender=? AND sender_lookup=?",
                        key,
                    )
                    total.insert(self.connection)
                self.connection.execute(f'DELETE FROM "{RouteSourceRow.declared_name}"')
                RouteSourceRow(1, identity, size, tail_digest).insert(self.connection)
            return True
        finally:
            if source is not None:
                source.close()

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
                f'''WITH requests AS (
                SELECT json_extract(value, '$.actor') AS actor,
                       json_extract(value, '$.target') AS target,
                       json_extract(value, '$.sender') AS sender,
                       json_extract(value, '$.sender_lookup') AS sender_lookup,
                       json_extract(value, '$.since') AS since FROM json_each(?)
            ), painted AS (
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
