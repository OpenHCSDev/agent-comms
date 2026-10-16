"""Disposable byte offsets for bounded JSONL history pages.

The bus remains authoritative. This index is advanced under its file lock and
is never used across a changed inode, shortened file, or changed indexed tail.
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal

from .coordination_database import CoordinationStore
from .errors import RelationViolationError
from .bus_projection import AppendCheckpoint
from .private_path import FileRevision
from .typed_table import Column, Index, SQLiteSchemaObject, TypedTable
from .wire_record import WireRecord

if TYPE_CHECKING:
    from .wire_log import OpenedWireSnapshot


class StaleBusPageIndexError(ValueError):
    """A disposable offset no longer identifies its claimed wire row."""


class OversizedIndexedBusRowError(StaleBusPageIndexError):
    """A warm indexed row exceeds an optional projection's byte budget."""


class PageIndexTable:
    """Disposable row offsets and their exact source revision."""


@dataclass(frozen=True)
class BusPageSource(PageIndexTable, TypedTable):
    revision: FileRevision
    tail: str
    singleton: Literal[1] = field(default=1, metadata={"sql": Column(primary_key=True)})

    def covers(self, stream: BinaryIO, source: FileRevision) -> bool:
        """A later append index may lend offsets within this original cut."""
        opened = FileRevision.from_stat(os.fstat(stream.fileno()))
        return (
            source.holds_cut(opened, source.size)
            and self.revision.identity == opened.identity
            and source.size <= self.revision.size <= opened.size
            and self.tail == AppendCheckpoint.fingerprint(stream, self.revision.size)
        )


@dataclass(frozen=True)
class BusPageRow(PageIndexTable, TypedTable):
    seq: int = field(metadata={"sql": Column(primary_key=True)})
    offset: int
    sender: str
    target: str
    indexes = (Index(("target", "seq")),)


class BusPageIndex:
    batch_bytes = 4 * 1024 * 1024

    def __init__(self, bus_path: Path, *, readonly: bool = False):
        self.bus_path = bus_path
        self.path = bus_path.with_name("bus_page_index.sqlite3")
        self.connection = (
            CoordinationStore.read_snapshot(self.path)
            if readonly
            else CoordinationStore.writer(self.path)
        )
        if not readonly:
            self.connection.execute("PRAGMA synchronous=FULL")
        try:
            with nullcontext() if readonly else self.connection:
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
                elif SQLiteSchemaObject.differences(actual, schema):
                    raise StaleBusPageIndexError("Bus page index requires the quiet runtime reset")
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> BusPageIndex:
        return self

    def __exit__(self, *_error: object) -> None:
        self.connection.close()

    @contextmanager
    def snapshot(self):
        """Pin coverage and offsets to one SQLite revision.

        Read-only indexes arrive with their snapshot already acquired. A
        writable index commits preparation first, then borrows this read
        transaction while consumers validate coverage and select offsets.
        """
        borrowed = self.connection.in_transaction
        try:
            if not borrowed:
                CoordinationStore.begin_snapshot(self.connection)
            yield self
        finally:
            if not borrowed and self.connection.in_transaction:
                self.connection.execute("ROLLBACK")

    def sync(self) -> bool:
        """Prepare a freshly opened source; incomplete source is an error."""
        from .wire_log import WireLog

        with WireLog(self.bus_path)._opened_wire_snapshot(need_sequence=False) as source:
            self.prepare(source)
        return True

    def prepare(self, opened: OpenedWireSnapshot) -> None:
        """Bring this disposable index up to the caller's original source cut."""
        while True:
            prepared = self._prepare(opened)
            if prepared is None:
                raise RelationViolationError("Cannot prepare a page index from an incomplete wire cut")
            saved, source, offset, rows = prepared
            if saved == source:
                return
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                if BusPageSource.one(self.connection, singleton=1) != saved:
                    continue
                if offset == 0:
                    self.connection.execute(f"DELETE FROM {BusPageRow.declared_name}")
                for row in rows:
                    row.insert(self.connection)
                source.upsert(self.connection)

    def _prepare(self, opened: OpenedWireSnapshot) -> tuple[BusPageSource | None, BusPageSource, int, tuple[BusPageRow, ...]] | None:
        """Read and decode the source before taking the SQLite writer lock."""
        with self.snapshot():
            saved = BusPageSource.one(self.connection, singleton=1)
            last_rows = BusPageRow.read(self.connection.execute(
                f"SELECT {BusPageRow._column_list(BusPageRow.columns())} "
                f"FROM {BusPageRow.declared_name} ORDER BY seq DESC LIMIT 1"
            ))
        stream, revision = opened.stream, opened.revision
        if stream is None:
            return None
        size = opened.boundary
        if size:
            stream.seek(size - 1)
            if stream.read(1) != b"\n":
                return None
        if saved is not None and saved.covers(stream, revision):
            return saved, saved, saved.revision.size, ()
        offset = saved.revision.size if saved is not None else 0
        valid = (
            saved is not None
            and saved.revision.holds_cut(revision, offset)
            and saved.tail == AppendCheckpoint.fingerprint(stream, offset)
        )
        if not valid:
            offset = 0
        if valid and offset == size:
            return saved, saved, offset, ()
        rows: list[BusPageRow] = []
        last_sequence = last_rows[0].seq if valid and last_rows else None
        stream.seek(offset)
        while stream.tell() < size and stream.tell() - offset < self.batch_bytes:
            row_offset = stream.tell()
            raw = stream.readline(min(WireRecord.max_row_bytes + 1, size - row_offset))
            WireRecord.require_row_size(
                raw, error_type=RelationViolationError,
                message="Bus row exceeds its declared byte limit",
            )
            if not raw.endswith(b"\n"):
                return None
            if not raw.strip():
                continue
            record = json.loads(raw)
            if not isinstance(record, Mapping):
                raise ValueError("JSONL bus row must be an object.")
            for message in opened.messages(record):
                if last_sequence is not None and message.seq <= last_sequence:
                    raise RelationViolationError("Wire sequences are not increasing.")
                rows.append(BusPageRow(message.seq, row_offset, message.sender, message.target))
                last_sequence = message.seq
        through = stream.tell()
        source = BusPageSource(replace(revision, size=through), AppendCheckpoint.fingerprint(stream, through))
        return saved, source, offset, tuple(rows)

    def invalidate(self) -> None:
        """Discard damaged derived coverage; prepare owns the subsequent rebuild."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            BusPageSource.delete(self.connection, singleton=1)

    def offsets(
        self,
        *,
        lower: int | None,
        upper: int | None,
        descending: bool,
        targets: frozenset[str] | None,
        before_offset: int | None = None,
    ) -> Generator[BusPageRow, None, None]:
        clauses: list[str] = []
        params: list[object] = []
        if targets is not None:
            if not targets:
                return
            clauses.append("target IN (" + ",".join("?" for _ in targets) + ")")
            params.extend(sorted(targets))
        yield from self._offsets(clauses, params, lower=lower, upper=upper,
                                 descending=descending, before_offset=before_offset)

    def conversation_offsets(
        self, first: frozenset[str], second: frozenset[str], *,
        lower: int | None, upper: int | None, descending: bool,
        before_offset: int | None = None,
    ) -> Generator[BusPageRow, None, None]:
        """Only the captured pair's rows, in the requested sequence order."""
        if not first or not second:
            return
        a, b = sorted(first), sorted(second)
        qa, qb = ",".join("?" for _ in a), ",".join("?" for _ in b)
        clauses = [f"((sender IN ({qa}) AND target IN ({qb})) OR "
                   f"(sender IN ({qb}) AND target IN ({qa})))"]
        yield from self._offsets(clauses, [*a, *b, *b, *a], lower=lower, upper=upper,
                                 descending=descending, before_offset=before_offset)

    def _offsets(
        self, clauses: list[str], params: list[object], *, lower: int | None,
        upper: int | None, descending: bool, before_offset: int | None,
    ) -> Generator[BusPageRow, None, None]:
        """One owner for source bounds, ordering and typed offset decoding."""
        if lower is not None:
            clauses.append("seq > ?")
            params.append(lower)
        if upper is not None:
            clauses.append("seq < ?")
            params.append(upper)
        if before_offset is not None:
            clauses.append("offset < ?")
            params.append(before_offset)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        direction = "DESC" if descending else "ASC"
        query = (
            f"SELECT {BusPageRow._column_list(BusPageRow.columns())} "
            f"FROM {BusPageRow.declared_name}"
            + where
            + f" ORDER BY seq {direction}"
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
