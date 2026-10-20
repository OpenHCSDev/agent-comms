"""Disposable byte offsets for bounded JSONL history pages.

The bus remains authoritative. SQLite's writer transaction owns index advancement;
coverage and offsets are never used across a changed inode, shortened file, or changed tail.
"""

from __future__ import annotations

import json
import typing
from collections.abc import Generator, Iterator, Mapping
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal

from .bus_activity_index import ChannelActivity
from .bus_display_index import DisplaySnapshot
from .coordination_database import CoordinationStore
from .errors import RelationViolationError
from .field_codec import MalformedRecord
from .private_path import FileRevision
from .read_basis import (
    ChannelDisplayScope,
    ConversationCursor,
    DMDisplayScope,
    MessageDisplayScope,
    ReadCursor,
)
from .typed_table import Column, Index, TypedRow, TypedStore, TypedTable

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
    singleton: typing.Annotated[Literal[1], Column(primary_key=True)] = field(default=1)

    def covers(self, snapshot: WireSnapshot) -> bool:
        return (snapshot.same_file(self.revision.identity)
                and snapshot.complete_prefix.boundary <= self.revision.size
                and snapshot.fingerprint(self.revision.size) == self.tail)


@dataclass(frozen=True)
class BusPageRow(PageIndexTable, TypedTable):
    seq: typing.Annotated[int, Column(primary_key=True)]
    offset: int
    sender: str
    target: str
    timestamp: float
    human: bool
    mentions: tuple[str, ...]
    indexes = (
        Index(("target", "seq")),
        Index(("offset",)),
    )

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
    last: typing.Annotated[float, Column(generated="MAX(0,COALESCE(MAX({timestamp}),0))")]
    human: typing.Annotated[
        float, Column(generated="MAX(0,COALESCE(MAX(CASE WHEN {human} THEN {timestamp} END),0))")
    ]


@dataclass(frozen=True)
class LatestConversation(TypedRow):
    target: str
    sender: str
    seq: int


class BusPageIndex(TypedStore):
    filename = "bus_page_index.sqlite3"
    rows = tuple(TypedTable.members_with(PageIndexTable))
    schema_error = StaleBusPageIndexError
    batch_bytes = 4 * 1024 * 1024

    def __init__(self, bus_path: Path):
        self.root = bus_path.parent
        self.bus_path = bus_path
        self._resources = ExitStack()

    @cached_property
    def connection(self):
        with ExitStack() as resources:
            db = resources.enter_context(self._attached(
                lambda: closing(CoordinationStore.read_snapshot(self.path))))
            if actual := self._schema(db):
                self.require_schema(actual)
            db.execute("ROLLBACK")
            self._resources.enter_context(resources.pop_all())
            return db

    def _open_writer(self, *, check_same_thread: bool = True):
        """Keep disposable-schema repair inside the store's WAL attachment boundary."""
        with ExitStack() as opening:
            db = opening.enter_context(closing(super()._open_writer(
                check_same_thread=check_same_thread)))
            try:
                self.require_schema(self._schema(db))
            except StaleBusPageIndexError:
                db.execute("BEGIN IMMEDIATE")
                for table in self.rows:
                    table.drop(db)
                    table.create(db)
                db.execute("COMMIT")
            opening.pop_all()
            return db

    def __enter__(self) -> BusPageIndex:
        return self

    def __exit__(self, *_error: object) -> None:
        self._resources.close()

    @contextmanager
    def covered_snapshot(self, opened: WireSnapshot):
        """Admit an existing index and retain the revision proving its coverage."""
        if not self.path.exists():
            yield False
            return
        with self.snapshot():
            actual = self._schema(self.connection)
            saved = BusPageSource.one(self.connection, singleton=1) if actual else None
            yield saved is not None and saved.covers(opened)

    def covers(self, opened: WireSnapshot) -> bool:
        with self.covered_snapshot(opened) as covered:
            return covered

    @contextmanager
    def covered_read(self, opened: WireSnapshot):
        """Pin a covered read; only the append or explicit sync builds offsets."""
        with self.covered_snapshot(opened) as covered:
            if not covered:
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

    def sync(self, opened: WireSnapshot) -> bool:
        """Advance the source supplied by its writer; never acquire another bus lock."""
        # Coverage admission opened a reader. Retire it before the writer so
        # SQLite can checkpoint on writer close, instead of leaving a cold
        # read-only attachment to reconstruct the entire WAL on every query.
        self._resources.close()
        self.__dict__.pop('connection', None)
        revision = opened.revision
        size = opened.complete_prefix.boundary
        while size:
            with self.writing() as db:
                saved = BusPageSource.one(db, singleton=1)
                if saved is not None and saved.covers(opened):
                    return True
                offset = saved.revision.size if saved is not None else 0
                valid = (
                    saved is not None
                    and saved.revision.holds_cut(revision, offset)
                    and saved.tail == opened.fingerprint(offset)
                )
                if not valid:
                    offset = 0
                    BusPageRow.delete(db)
                last_rows = BusPageRow.select(db, order_by=("seq",), descending=True, limit=1)
                last_sequence = last_rows[0].seq if last_rows else None
                for record, row_offset, _ in opened.rows(offset, min(size, offset + self.batch_bytes)):
                    for message in opened.messages(record):
                        if last_sequence is not None and message.seq <= last_sequence:
                            raise RelationViolationError("Wire sequences are not increasing.")
                        BusPageRow(message.seq, row_offset, message.sender, message.target,
                                   message.timestamp, message.sender_role.page_human_sender(),
                                   tuple(mention.thread for mention in message.mentions)).insert(db)
                        last_sequence = message.seq
                through = opened.position
                BusPageSource(replace(revision, size=through), opened.fingerprint(through)).upsert(db)
        return True

    def invalidate(self) -> None:
        """Discard damaged derived coverage under the index's writer exclusion."""
        with self.writing() as db:
            BusPageSource.delete(db, singleton=1)

    def offsets(
        self,
        *,
        lower: int | None,
        upper: int | None,
        descending: bool,
        predicate: tuple[str, tuple],
        before_offset: int | None = None, after_offset: int | None = None,
    ) -> Generator[BusPageRow]:
        condition, bindings = predicate
        clauses: list[str] = [f"({condition})"]
        params: list[object] = list(bindings)
        if after_offset is not None:
            clauses.append("offset >= ?")
            params.append(after_offset)
        yield from self._offsets(clauses, params, lower=lower, upper=upper,
                                 descending=descending, before_offset=before_offset)

    def sequence_offsets(
        self, sequences: frozenset[int], *, before_offset: int,
    ) -> Iterator[BusPageRow]:
        """Exactly the named sequences' rows, ascending."""
        yield from self._offsets(["seq IN (" + ",".join("?" for _ in sequences) + ")"],
                                 [*sorted(sequences)], lower=None, upper=None,
                                 descending=False, before_offset=before_offset)

    def _offsets(
        self, clauses: list[str], params: list[object], *, lower: int | None,
        upper: int | None, descending: bool, before_offset: int | None,
    ) -> Generator[BusPageRow]:
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

    def latest_target(self, target: str, before_offset: int) -> BusPageRow | None:
        rows = BusPageRow.select(self.connection, where="target=? AND offset<?",
                                 parameters=(target, before_offset), order_by=("seq",),
                                 descending=True, limit=1)
        return rows[0] if rows else None

    def latest_by_conversation(self, scope: MessageDisplayScope, before_offset: int,
                               *, through: int) -> list[LatestConversation]:
        predicate, parameters = scope.index_predicate
        return LatestConversation.read(self.connection.execute(
            f'SELECT target, sender, MAX(seq) AS seq '
            f'FROM "{BusPageRow.declared_name}" WHERE offset<? AND seq<=? AND ({predicate}) '
            f'GROUP BY target, sender', (before_offset, through, *parameters),
        ))

    def proved_read_prefix(self, scope: ChannelDisplayScope | DMDisplayScope, previous: ConversationCursor,
                           sequences: tuple[int, ...], before_offset: int) -> int:
        through = max((*sequences, *previous.exceptions, previous.high_water))
        predicate, parameters = scope.index_predicate
        missing, = self.connection.execute(
            f'SELECT MIN(seq) FROM "{BusPageRow.declared_name}" WHERE offset<? AND ({predicate}) '
            'AND seq>? AND seq<=? AND seq NOT IN (SELECT value FROM json_each(?))',
            (before_offset, *parameters, previous.high_water, through,
             json.dumps((*sequences, *previous.exceptions))),
        ).fetchone()
        return through if missing is None else missing - 1

    def display_unread(self, scopes: tuple[ChannelDisplayScope, ...], viewer_names: frozenset[str],
                       seen: ReadCursor, before_offset: int, snapshot: RegistryProvenance) -> dict[str, int]:
        counts = {}
        for scope in scopes:
            bindings = ReadCursor.sql_bindings(snapshot, {"viewer": seen.counted(scope, snapshot)})
            predicate, parameters = scope.index_predicate
            counts[scope.channel] = self.connection.execute(
                f'WITH {ReadCursor.sql("?")}, requests AS ('
                f'SELECT \'viewer\' AS actor,* FROM "{BusPageRow.declared_name}" '
                'WHERE seq > ? AND offset < ? '
                f'AND ({predicate}) AND sender NOT IN (SELECT value FROM json_each(?))), '
                f'{ReadCursor.joined_sql} SELECT count(*) FROM cursors WHERE {ReadCursor.unread_sql}',
                (*bindings.values(), seen.lower_bound(scope, snapshot), before_offset, *parameters,
                 json.dumps(tuple(viewer_names))),
            ).fetchone()[0]
        return counts


class EmptyBusPageIndex(BusPageIndex):
    """The query result of a zero-byte cut; no SQL or file is fabricated."""

    def __init__(self) -> None:
        pass

    def _offsets(self, *args, **kwargs):
        yield from ()

    def latest_target(self, target: str, before_offset: int) -> BusPageRow | None:
        return None

    def latest_by_conversation(self, scope: MessageDisplayScope, before_offset: int,
                               *, through: int) -> list[LatestConversation]:
        return []

    def display_snapshot(self, scopes, boundary):
        return DisplaySnapshot.empty(scopes)

    def display_unread(self, scopes: tuple[ChannelDisplayScope, ...], viewer_names: frozenset[str],
                       seen: ReadCursor, before_offset: int, snapshot: RegistryProvenance) -> dict[str, int]:
        return dict.fromkeys((scope.channel for scope in scopes), 0)
