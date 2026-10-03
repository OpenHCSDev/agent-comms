"""Disposable byte offsets for bounded JSONL history pages.

The bus remains authoritative. This index is advanced under its file lock and
is never used across a changed inode, shortened file, or changed indexed tail.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Generator, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, Literal

from .typed_table import Column, Index, SQLiteSchemaObject, TypedTable


class StaleBusPageIndexError(ValueError):
    """A disposable offset no longer identifies its claimed wire row."""


class OversizedIndexedBusRowError(StaleBusPageIndexError):
    """A warm indexed row exceeds an optional projection's byte budget."""


class PageIndexTable:
    """Disposable row offsets and their exact source revision."""


@dataclass(frozen=True)
class BusPageSource(PageIndexTable, TypedTable):
    identity: tuple[int, int, int, int]
    offset: int = field(metadata={"sql": Column(check="offset>=0")})
    tail: str
    singleton: Literal[1] = field(default=1, metadata={"sql": Column(primary_key=True)})


@dataclass(frozen=True)
class BusPageRow(PageIndexTable, TypedTable):
    seq: int
    offset: int
    sender: str
    target: str
    id: int | None = field(default=None, metadata={"sql": Column(primary_key=True)})
    indexes = (Index(("seq", "id")), Index(("target", "seq", "id")))


class BusPageIndex:
    def __init__(self, bus_path: Path, *, readonly: bool = False):
        self.bus_path = bus_path
        self.path = bus_path.with_name("bus_page_index.sqlite3")
        self.connection = sqlite3.connect(
            self.path.as_uri() + "?mode=ro" if readonly else self.path,
            uri=readonly, timeout=30,
        )
        self.connection.execute("PRAGMA query_only=ON" if readonly else "PRAGMA synchronous=FULL")
        try:
            with self.connection:
                if not readonly:
                    self.connection.execute("BEGIN IMMEDIATE")
                actual = SQLiteSchemaObject.read(
                    self.connection.execute(
                        "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                )
                schema = {
                    name: sql
                    for table in TypedTable.members_with(PageIndexTable)
                    for name, sql in table.schema_objects().items()
                }
                if not actual and not readonly:
                    for statement in schema.values():
                        self.connection.execute(statement)
                elif {row.name: row.sql for row in actual} != schema:
                    raise StaleBusPageIndexError("Bus page index requires the quiet runtime reset")
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> BusPageIndex:
        return self

    def __exit__(self, *_error: object) -> None:
        self.connection.close()

    @staticmethod
    def _tail(stream: BinaryIO, offset: int) -> str:
        start = max(0, offset - 4096)
        stream.seek(start)
        return sha256(stream.read(offset - start)).hexdigest()

    def sync(self) -> bool:
        """Return false for an unfinished tail, which the caller scans normally."""
        try:
            stream = self.bus_path.open("rb")
        except FileNotFoundError:
            return False
        with stream:
            stat = os.fstat(stream.fileno())
            size = stat.st_size
            if size:
                stream.seek(size - 1)
                if stream.read(1) != b"\n":
                    return False
            identity = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns)
            try:
                saved = BusPageSource.one(self.connection, singleton=1)
            except (ValueError, TypeError):
                saved = None  # Rebuild damaged current-format derived evidence from its source.
            offset = saved.offset if saved is not None else 0
            valid = (
                saved is not None
                and 0 <= offset <= size
                and saved.identity[:2] == identity[:2]
                and (offset != size or saved.identity[2:] == identity[2:])
                and saved.tail == self._tail(stream, offset)
            )
            if not valid:
                offset = 0
            if valid and offset == size:
                return True
            with self.connection:
                if not valid:
                    self.connection.execute(f"DELETE FROM {BusPageRow.declared_name}")
                    last_sequence = None
                else:
                    last_rows = BusPageRow.read(
                        self.connection.execute(
                            f"SELECT {BusPageRow._column_list(BusPageRow.columns())} "
                            f"FROM {BusPageRow.declared_name} ORDER BY id DESC LIMIT 1"
                        )
                    )
                    last_sequence = last_rows[0].seq if last_rows else None
                stream.seek(offset)
                while stream.tell() < size:
                    row_offset = stream.tell()
                    raw = stream.readline()
                    if not raw.endswith(b"\n"):
                        return False
                    if not raw.strip():
                        continue
                    record = json.loads(raw)
                    if not isinstance(record, Mapping):
                        raise ValueError("JSONL bus row must be an object.")
                    # Full validation on the newly indexed segment. A warm
                    # read validates each selected record again from the bus.
                    from .wire_record import WireRecord

                    for message in WireRecord.public_from_wire(record).messages():
                        if last_sequence is not None and message.seq <= last_sequence:
                            # The page collector uses wire order. A cache sorted
                            # by sequence must not hide malformed source order.
                            raise StaleBusPageIndexError("Wire sequences are not increasing.")
                        BusPageRow(message.seq, row_offset, message.sender, message.target).insert(
                            self.connection
                        )
                        last_sequence = message.seq
                BusPageSource(identity, size, self._tail(stream, size)).upsert(self.connection)
            return True

    def current(self) -> bool:
        """Read-only warm-cache check; never rebuild the bus on a wake."""
        try:
            with self.bus_path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                size = stat.st_size
                if size:
                    stream.seek(size - 1)
                    if stream.read(1) != b"\n":
                        return False
                saved = BusPageSource.one(self.connection, singleton=1)
                return saved is not None and saved == BusPageSource(
                    (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns),
                    size,
                    self._tail(stream, size),
                )
        except (OSError, ValueError, TypeError, sqlite3.DatabaseError):
            return False

    def offsets(
        self,
        *,
        lower: int | None,
        upper: int | None,
        descending: bool,
        targets: frozenset[str] | None,
    ) -> Generator[BusPageRow, None, None]:
        clauses: list[str] = []
        params: list[object] = []
        if lower is not None:
            clauses.append("seq > ?")
            params.append(lower)
        if upper is not None:
            clauses.append("seq < ?")
            params.append(upper)
        if targets is not None:
            if not targets:
                return
            clauses.append("target IN (" + ",".join("?" for _ in targets) + ")")
            params.extend(sorted(targets))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        direction = "DESC" if descending else "ASC"
        query = (
            f"SELECT {BusPageRow._column_list(BusPageRow.columns())} "
            f"FROM {BusPageRow.declared_name}"
            + where
            + f" ORDER BY seq {direction}, id {direction}"
        )
        rows = iter(BusPageRow.iterate(self.connection.execute(query, params)))
        while True:
            try:
                row = next(rows)
            except StopIteration:
                return
            except (ValueError, TypeError) as error:
                raise StaleBusPageIndexError("Indexed row has invalid fields") from error
            yield row

    @staticmethod
    def record(
        stream: BinaryIO, row: BusPageRow, *, max_bytes: int | None = None
    ) -> tuple[Mapping, int]:
        stream.seek(row.offset)
        raw = stream.readline(max_bytes + 1 if max_bytes is not None else -1)
        if max_bytes is not None and len(raw) > max_bytes:
            raise OversizedIndexedBusRowError("Indexed bus row exceeds advisory byte budget.")
        try:
            record = json.loads(raw)
        except ValueError as error:
            raise StaleBusPageIndexError("Indexed bus row is invalid.") from error
        if (
            not isinstance(record, Mapping)
            or int(record.get("seq", 0)) != row.seq
            or record.get("from") != row.sender
            or record.get("to") != row.target
        ):
            raise StaleBusPageIndexError("Indexed bus row changed.")
        return record, len(raw)
