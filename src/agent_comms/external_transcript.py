"""Transcript items a CLI harness publishes for its sessionless bus thread.

A thread without a native session has no session file of its own; the CLI
bridge publishes each finished item (a reply, a reasoning summary, a tool call,
a compaction) here once. Finished items are append-only, so the thread's page
merges them with its bus messages exactly as a native session's records are
merged: the item's sequence is its position. What the CLI is doing before an
item finishes is the thread's activity, reported through Core's activity log.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager
from sqlite3 import Connection
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from .declared_family import DeclaredFamily
from .native_transcript import RecordsCut
from .thread_identity import ThreadIncarnation
from .transcript_events import TranscriptEvent, TurnDigest
from .transcript_sources import TranscriptSource
from .typed_table import Column, TypedRow, TypedStore, TypedTable
from .wire_watch import WatchSource, WireWatch


@dataclass(frozen=True)
class ExternalTurn(TypedRow):
    turn_id: str


class ExternalPage(DeclaredFamily):
    """The continuation belonging to the oldest published item."""

    def has_older(self, offset: int, row) -> bool:
        return offset >= row.seq

    def request(self, transcript, row) -> bool:
        return False

    def frames(self, row) -> tuple:
        return ()


@dataclass(frozen=True)
class NoEarlierPage(ExternalPage):
    pass


@dataclass(frozen=True)
class EarlierPage(ExternalPage):
    cursor: str

    def has_older(self, offset: int, row) -> bool:
        return True

    def request(self, transcript, row) -> bool:
        with transcript.writing() as connection:
            if transcript.oldest(connection) != [row]:
                return True  # The read retries against the newly published prefix.
            row.continue_with(connection, RequestedEarlierPage(self.cursor))
        return True


@dataclass(frozen=True)
class RequestedEarlierPage(EarlierPage):
    """Demand is a transition of the stored continuation, not a second cursor store."""

    def request(self, transcript, row) -> bool:
        return True  # A deferred read keeps the request without another write.

    def frames(self, row) -> tuple:
        return (row,)


@dataclass(frozen=True)
class ExternalPageRow(TypedRow):
    thread: str = field(metadata={"sql": Column(primary_key=True)})
    turn_id: str = field(metadata={"sql": Column(primary_key=True)})
    item_id: str = field(metadata={"sql": Column(primary_key=True)})
    seq: int
    earlier: ExternalPage = field(default_factory=NoEarlierPage, kw_only=True)

    def has_older(self, offset: int) -> bool:
        return self.earlier.has_older(offset, self)

    def continue_with(self, connection, earlier: ExternalPage) -> None:
        ExternalItemRow.update(connection, where="thread=? AND turn_id=? AND item_id=?",
                               parameters=(self.thread, self.turn_id, self.item_id), earlier=earlier)


@dataclass(frozen=True)
class ExternalItemRow(ExternalPageRow, TypedTable):
    """A stable item position; older pages prepend without moving saved cursors."""

    events: tuple[TranscriptEvent, ...]
    unique = (("thread", "seq"),)

    @classmethod
    def recent_turn_ids(cls, connection: Connection, thread: str, last: int) -> tuple[str, ...]:
        """Find recent identities without decoding bodies or grouping the full history."""
        identities: dict[str, None] = {}
        with closing(cls.stream(connection, row_type=ExternalTurn, thread=thread,
                                order_by=("seq",), descending=True)) as rows:
            for row in rows:
                identities[row.turn_id] = None
                if len(identities) >= last:
                    break
        return tuple(identities)


@dataclass(frozen=True)
class ExternalDraftRow(TypedTable):
    """The current CLI item, replaced as it grows and retired at completion."""

    thread: str = field(metadata={"sql": Column(primary_key=True)})
    turn_id: str
    item_id: str
    events: tuple[TranscriptEvent, ...]


@dataclass(frozen=True)
class ExternalRecord:
    """An item at its page position, in the shape the page merge consumes."""

    seq: int
    events: tuple[TranscriptEvent, ...]

    @property
    def start(self) -> int:
        return self.seq - 1

    @property
    def end(self) -> int:
        return self.seq

    @property
    def size(self) -> int:
        return sum(event.text_size for event in self.events)

    def incomplete_tail(self, through: int) -> bool:
        return False  # Only finished items are records.


@dataclass(frozen=True)
class ExternalCut(RecordsCut, TypedRow):
    """The thread's newest finished item sequence when read."""

    offset: int = field(metadata={"sql": Column(generated="coalesce(max({seq}), 0)")})

    def contains(self, current: ExternalCut, through: int) -> bool:
        # Finished items are append-only: later items belong to the next update.
        return current.offset >= self.offset

    def draft_current(self, current: RecordsCut) -> bool:
        # Finishing a CLI item retires its draft atomically. Its current draft
        # must not be paired with a page captured before that completion.
        return self == current


@dataclass(frozen=True, slots=True)
class ExternalTranscriptRead(TranscriptSource):
    """One SQLite snapshot of a sessionless transcript's finished and draft rows."""

    transcript: ExternalTranscript
    connection: Connection | None

    @property
    def lookup(self) -> str:
        return self.transcript.lookup

    @property
    def start(self) -> int:
        if self.connection is None:
            return 0
        rows = self.transcript.oldest(self.connection)
        return rows[0].seq - 1 if rows else 0

    def has_records(self, through: int) -> bool:
        return True  # Zero and negative positions name real prepended items.

    def valid_offset(self, offset: int, through: int) -> bool:
        return offset <= through

    def has_older(self, offset: int) -> bool:
        if self.connection is None:
            return False
        rows = self.transcript.oldest(self.connection)
        return bool(rows) and rows[0].has_older(offset)

    def current_draft(self) -> ExternalDraftRow | None:
        if self.connection is None:
            return None
        return ExternalDraftRow.one(self.connection, thread=self.lookup)

    def cut(self) -> ExternalCut:
        if self.connection is None:
            return ExternalCut(0)
        return self.transcript._cut(self.connection)

    def _records(self, where: str, parameters: tuple, *,
                 descending: bool) -> Iterator[ExternalRecord]:
        if self.connection is None:
            return
        rows = ExternalItemRow.stream(
            self.connection, where=where, parameters=parameters, order_by=("seq",),
            descending=descending, thread=self.lookup)
        try:
            for row in rows:
                yield ExternalRecord(row.seq, row.events)
        finally:
            rows.close()

    def turns(self, last: int) -> tuple[TurnDigest, ...]:
        if self.connection is None:
            return ()
        return tuple(
            TurnDigest(turn_id, tuple(
                event
                for row in ExternalItemRow.select(
                    self.connection, thread=self.lookup, turn_id=turn_id, order_by=("seq",))
                for event in row.events))
            for turn_id in ExternalItemRow.recent_turn_ids(self.connection, self.lookup, last))

    def forward(self, after: int, through: int) -> Iterator[ExternalRecord]:
        return self._records("seq > ? AND seq <= ?", (after, through), descending=False)

    def reverse(self, before: int) -> Iterator[ExternalRecord]:
        return self._records("seq <= ?", (before,), descending=True)

@dataclass(frozen=True, slots=True)
class ExternalTranscript(TranscriptSource, TypedStore):
    """One sessionless thread's CLI items: published by its bridge, paged by Core."""

    root: Path
    incarnation: ThreadIncarnation
    filename: ClassVar[str] = "external_transcripts.sqlite3"
    rows: ClassVar[tuple[type[TypedTable], ...]] = (ExternalItemRow, ExternalDraftRow)
    # The CLI keeps its own items; a lost tail is republished when a watch starts.
    synchronous: ClassVar[str] = "NORMAL"

    @classmethod
    def source_paths(cls, root: Path) -> tuple[Path, ...]:
        database = cls.path_in(root)
        return database, database.with_name(database.name + "-wal")

    @property
    def lookup(self) -> str:
        return self.incarnation.lookup

    def finish(self, *, turn_id: str, item_id: str,
               events: tuple[TranscriptEvent, ...]) -> None:
        """Append a finished item once; an item with no presentation adds nothing."""
        key = dict(thread=self.lookup, turn_id=turn_id, item_id=item_id)
        with self.writing() as connection:
            if events and not ExternalItemRow.count(connection, **key):
                ExternalItemRow(seq=self._cut(connection).offset + 1, events=events,
                                **key).insert(connection)
            ExternalDraftRow.delete(connection, **key)

    def oldest(self, connection) -> list[ExternalPageRow]:
        return ExternalItemRow.select(connection, thread=self.lookup, row_type=ExternalPageRow,
                                      order_by=("seq",), limit=1)

    def publish_page(self, items, earlier: ExternalPage) -> None:
        """Publish the newest snapshot once, including empty items that own a continuation."""
        with self.writing() as connection:
            for item in items:
                key = dict(thread=self.lookup, turn_id=item.turn_id, item_id=item.item_id)
                if not ExternalItemRow.count(connection, **key):
                    ExternalItemRow(seq=self._cut(connection).offset + 1,
                                    events=item.events, **key).insert(connection)
            rows = self.oldest(connection)
            if rows and items and (rows[0].turn_id, rows[0].item_id) == (items[0].turn_id, items[0].item_id):
                rows[0].continue_with(connection, earlier)

    def request_earlier(self, offset: int) -> bool:
        if not self.path.exists():
            return False
        with self.snapshot() as connection:
            rows = self.oldest(connection)
        if not rows or offset >= rows[0].seq:
            return False
        row, = rows
        return row.has_older(offset) and row.earlier.request(self, row)

    def pending(self, request) -> bool:
        with self.snapshot() as connection:
            return bool(self._requested(connection, request))

    def _requested(self, connection, request) -> list[ExternalPageRow]:
        return [row for row in self.oldest(connection)
                if (row.turn_id, row.item_id, row.earlier) ==
                (request.turn_id, request.item_id, request.request)]

    def prepend(self, request, items, earlier: ExternalPage) -> None:
        with self.writing() as connection:
            rows = self._requested(connection, request)
            if not rows:
                return  # An already fulfilled watch frame cannot publish twice.
            anchor = rows[0]
            anchor.continue_with(connection, NoEarlierPage())
            seq = anchor.seq
            for item in reversed(items):
                key = dict(thread=self.lookup, turn_id=item.turn_id, item_id=item.item_id)
                if not ExternalItemRow.count(connection, **key):
                    seq -= 1
                    ExternalItemRow(seq=seq, events=item.events, **key).insert(connection)
            oldest, = self.oldest(connection)
            oldest.continue_with(connection, earlier)

    async def watch_requests(self, emit) -> None:
        previous = ()
        async for _ in WireWatch.observations(*(WatchSource.modified_file(path)
                                               for path in self.source_paths(self.root))):
            current = ()
            if self.path.exists():
                with self.snapshot() as connection:
                    current = tuple(frame for row in self.oldest(connection)
                                    for frame in row.earlier.frames(row))
            for frame in current:
                if frame not in previous:
                    emit(frame)
            previous = current

    def draft(self, *, turn_id: str, item_id: str,
              events: tuple[TranscriptEvent, ...]) -> None:
        with self.writing() as connection:
            ExternalDraftRow(self.lookup, turn_id, item_id, events).upsert(connection)

    def end_turn(self) -> None:
        if not self.path.exists():
            return
        with self.writing() as connection:
            ExternalDraftRow.delete(connection, thread=self.lookup)

    @contextmanager
    def reading(self):
        """Retain one read transaction across cut, page records, and draft."""
        if not self.path.exists():
            yield ExternalTranscriptRead(self, None)
            return
        with self.snapshot() as connection:
            yield ExternalTranscriptRead(self, connection)

    def _cut(self, connection) -> ExternalCut:
        return ExternalItemRow.select(connection, row_type=ExternalCut, thread=self.lookup)[0]

    def current_draft(self) -> ExternalDraftRow | None:
        with self.reading() as records:
            return records.current_draft()

    def cut(self) -> ExternalCut:
        with self.reading() as records:
            return records.cut()


