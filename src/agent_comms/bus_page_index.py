"""Disposable byte offsets for bounded JSONL history pages.

The bus remains authoritative. This index is advanced under its file lock and
is never used across a changed inode, shortened file, or changed indexed tail.
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator, Iterator, Mapping
from contextlib import ExitStack, closing, contextmanager
from functools import cached_property
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal

from .field_codec import MalformedRecord
from .bus_activity_index import ChannelActivity
from .bus_display_index import DisplaySnapshot
from .coordination_database import CoordinationStore
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .bus_projection import AppendCheckpoint
from .private_path import FileRevision
from .read_basis import ChannelDisplayScope, ReadCursor
from .store_files import SharedLock, _store_lock
from .typed_table import Column, Index, SQLiteSchemaObject, TypedRow, TypedTable
from .wire_record import WireRecord

if TYPE_CHECKING:
    from .registry_provenance import RegistryProvenance
    from .wire_log import WireSnapshot


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

    def covers(self, snapshot: WireSnapshot) -> bool:
        return (snapshot.same_file(self.revision.identity)
                and snapshot.complete_prefix.boundary <= self.revision.size
                and snapshot.fingerprint(self.revision.size) == self.tail)


@dataclass(frozen=True)
class BusPageRow(PageIndexTable, TypedTable):
    seq: int = field(metadata={"sql": Column(primary_key=True)})
    offset: int
    sender: str
    target: str
    timestamp: float
    human: bool
    mentions: tuple[str, ...]
    indexes = (Index(("target", "seq")), Index(("offset",)),)

    def record(self, stream: BinaryIO, *, max_bytes: int | None = None) -> tuple[Mapping, int]:
        stream.seek(self.offset)
        raw = stream.readline(max_bytes + 1 if max_bytes is not None else -1)
        if max_bytes is not None and len(raw) > max_bytes:
            raise OversizedIndexedBusRowError("Indexed bus row exceeds advisory byte budget.")
        try:
            record = json.loads(raw)
        except ValueError as error:
            raise StaleBusPageIndexError("Indexed bus row is invalid.") from error
        if (
            not isinstance(record, Mapping)
            or int(record.get("seq", 0)) != self.seq
            or record.get("from") != self.sender
            or record.get("to") != self.target
        ):
            raise StaleBusPageIndexError("Indexed bus row changed.")
        return record, len(raw)


@dataclass(frozen=True)
class BusPageMetrics(TypedRow):
    last: float = field(metadata={"sql": Column(generated="MAX(0,COALESCE(MAX({timestamp}),0))")})
    human: float = field(metadata={"sql": Column(generated="MAX(0,COALESCE(MAX(CASE WHEN {human} THEN {timestamp} END),0))")})


class BusPageIndex:
    batch_bytes = 4 * 1024 * 1024

    def __init__(self, bus_path: Path):
        self.bus_path = bus_path
        self.path = bus_path.with_name("bus_page_index.sqlite3")
        self._resources = ExitStack()

    @cached_property
    def connection(self):
        with ExitStack() as resources:
            db = resources.enter_context(closing(CoordinationStore.read_snapshot(self.path)))
            self._schema(db)
            db.execute("ROLLBACK")
            self._resources.enter_context(resources.pop_all())
            return db

    @staticmethod
    def _schema(db, *, writer: bool = False):
        actual = SQLiteSchemaObject.read(db.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ))
        expected = {name: sql for table in TypedTable.members_with(PageIndexTable)
                    for name, sql in table.schema_objects().items()}
        if actual and SQLiteSchemaObject.differences(actual, expected):
            if not writer:
                raise StaleBusPageIndexError("Bus page index schema is not its declaration")
            # Offsets derive from the bus: the writer discards a schema other
            # than the declaration and rebuilds it under its lock.
            for table in TypedTable.members_with(PageIndexTable):
                table.drop(db)
            actual = ()
        return actual, expected

    def __enter__(self) -> BusPageIndex:
        return self

    def __exit__(self, *_error: object) -> None:
        self._resources.close()

    def covers(self, opened: WireSnapshot) -> bool:
        if not self.path.exists():
            return False
        with self.snapshot():
            actual, _ = self._schema(self.connection)
            saved = BusPageSource.one(self.connection, singleton=1) if actual else None
            return saved is not None and saved.covers(opened)


    @contextmanager
    def prepared(self, opened: WireSnapshot):
        with _store_lock(self.bus_path, mode=SharedLock()):
            if self.covers(opened):
                with self.snapshot():
                    yield
                return
        opened.metadata.access.require_append()
        self.sync()
        with _store_lock(self.bus_path, mode=SharedLock()), self.snapshot():
            if not self.covers(opened):
                raise StaleBusPageIndexError("Page index does not cover its opened source")
            yield

    @contextmanager
    def snapshot(self):
        """Pin coverage and offsets to one SQLite revision."""
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
        from .wire_log import CurrentSnapshotOpening, WireLog

        with WireLog(self.bus_path).opened_wire_snapshot(opening=CurrentSnapshotOpening()) as source, _store_lock(self.bus_path):
            self.prepare(source)
        return True

    def prepare(self, opened: WireSnapshot) -> None:
        """Bring this disposable index up to the caller's original source cut."""
        while opened.complete_prefix.boundary:
            saved, source, offset, rows = self._prepare(opened)
            if saved == source:
                return
            with self._writing() as db:
                if offset == 0:
                    BusPageRow.delete(db)
                for row in rows:
                    row.insert(db)
                source.upsert(db)

    def _prepare(self, opened: WireSnapshot) -> tuple[BusPageSource | None, BusPageSource, int, tuple[BusPageRow, ...]]:
        """Read and decode the source before taking the SQLite writer lock."""
        saved, last_rows = None, ()
        if self.path.exists():
            with self.snapshot():
                actual, _ = self._schema(self.connection)
                if actual:
                    saved = BusPageSource.one(self.connection, singleton=1)
                    last_rows = BusPageRow.select(self.connection, order_by=("seq",),
                                                  descending=True, limit=1)
        revision = opened.revision
        size = opened.complete_prefix.boundary
        if saved is not None and saved.covers(opened):
            return saved, saved, saved.revision.size, ()
        offset = saved.revision.size if saved is not None else 0
        valid = (
            saved is not None
            and saved.revision.holds_cut(revision, offset)
            and saved.tail == opened.fingerprint(offset)
        )
        if not valid:
            offset = 0
        rows: list[BusPageRow] = []
        last_sequence = last_rows[0].seq if valid and last_rows else None
        through = offset
        for record, row_offset, through in opened.rows(offset, min(size, offset + self.batch_bytes)):
            for message in opened.messages(record):
                if last_sequence is not None and message.seq <= last_sequence:
                    raise RelationViolationError("Wire sequences are not increasing.")
                rows.append(BusPageRow(message.seq, row_offset, message.sender, message.target,
                                       message.timestamp, message.sender_role.page_human_sender(),
                                       tuple(mention.thread for mention in message.mentions)))
                last_sequence = message.seq
        through = opened.position
        source = BusPageSource(replace(revision, size=through), opened.fingerprint(through))
        return saved, source, offset, tuple(rows)

    def invalidate(self) -> None:
        """Discard damaged derived coverage; prepare owns the subsequent rebuild."""
        with _store_lock(self.bus_path), self._writing() as db:
            BusPageSource.delete(db, singleton=1)

    @contextmanager
    def _writing(self):
        with closing(CoordinationStore.writer(self.path)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            actual, expected = self._schema(db, writer=True)
            if not actual:
                for statement in expected.values():
                    db.execute(statement)
            yield db

    def offsets(
        self,
        *,
        lower: int | None,
        upper: int | None,
        descending: bool,
        targets: frozenset[str] | None,
        before_offset: int | None = None, after_offset: int | None = None,
    ) -> Generator[BusPageRow, None, None]:
        clauses: list[str] = []
        params: list[object] = []
        if after_offset is not None:
            clauses.append("offset >= ?")
            params.append(after_offset)
        if targets is not None:
            if not targets:
                return
            clauses.append("target IN (" + ",".join("?" for _ in targets) + ")")
            params.extend(sorted(targets))
        yield from self._offsets(clauses, params, lower=lower, upper=upper,
                                 descending=descending, before_offset=before_offset)

    def sequence_offsets(
        self, sequences: frozenset[int], *, before_offset: int,
    ) -> Iterator[BusPageRow]:
        """Exactly the named sequences' rows, ascending."""
        yield from self._offsets(["seq IN (" + ",".join("?" for _ in sequences) + ")"],
                                 [*sorted(sequences)], lower=None, upper=None,
                                 descending=False, before_offset=before_offset)

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
        with closing(BusPageRow.stream(self.connection, where=" AND ".join(clauses) or "1",
                     parameters=tuple(params), order_by=("seq",), descending=descending)) as rows:
            try:
                yield from rows
            except MalformedRecord as error:
                raise StaleBusPageIndexError("Indexed row has invalid fields") from error

    def display_snapshot(self, scopes: tuple[ChannelDisplayScope, ...], before_offset: int) -> DisplaySnapshot:
        snapshot = DisplaySnapshot.empty(scopes)
        for scope in scopes:
            predicate, parameters = scope.index_predicate
            row, = BusPageRow.select(self.connection, row_type=BusPageMetrics,
                where=f"offset < ? AND ({predicate})",
                parameters=(before_offset, *parameters))
            snapshot.activity[scope.channel] = ChannelActivity(row.last, row.human)
        return snapshot

    def display_unread(self, scopes: tuple[ChannelDisplayScope, ...], viewer_names: frozenset[str],
                       seen: ReadCursor, before_offset: int, snapshot: RegistryProvenance) -> dict[str, int]:
        counts = {}
        try:
            for scope in scopes:
                self.connection.create_function(
                    "read_cursor", 2,
                    lambda sender, target: json.dumps(FieldCodec.encode(
                        seen.for_conversation(scope.read_key(sender, target, snapshot)),
                    )), deterministic=True,
                )
                predicate, parameters = scope.index_predicate
                counts[scope.channel] = BusPageRow.count(self.connection,
                    where=f"seq > ? AND offset < ? AND ({predicate}) "
                          "AND sender NOT IN (SELECT value FROM json_each(?)) "
                          "AND seq > json_extract(read_cursor(sender,target),'$.high_water') "
                          "AND seq NOT IN (SELECT value FROM "
                          "json_each(json_extract(read_cursor(sender,target),'$.exceptions')))",
                    parameters=(seen.lower_bound(scope, snapshot), before_offset, *parameters,
                                json.dumps(tuple(viewer_names))))
        finally:
            self.connection.create_function("read_cursor", 2, None)
        return counts


class EmptyBusPageIndex(BusPageIndex):
    """The query result of a zero-byte cut; no SQL or file is fabricated."""

    def __init__(self):
        pass

    def _offsets(self, *args, **kwargs):
        yield from ()

    def display_snapshot(self, scopes, boundary):
        return DisplaySnapshot.empty(scopes)

    def display_unread(self, scopes: tuple[ChannelDisplayScope, ...], viewer_names: frozenset[str],
                       seen: ReadCursor, before_offset: int, snapshot: RegistryProvenance) -> dict[str, int]:
        return dict.fromkeys((scope.channel for scope in scopes), 0)
