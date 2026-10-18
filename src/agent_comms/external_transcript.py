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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from uuid import uuid4

from .declared_family import DeclaredFamily
from .native_transcript import RecordsCut
from .thread_identity import ThreadIncarnation
from .transcript_events import TranscriptEvent, TurnDigest
from .transcript_sources import NoDraft, TranscriptDraft, TranscriptSource
from .typed_table import Column, TypedRow, TypedStore, TypedTable
from .wire_watch import WatchSource, WireWatch

if TYPE_CHECKING:
    from .turn_digests import TurnBound


@dataclass(frozen=True)
class ExternalTurn(TypedRow):
    turn_id: str


class ExternalPage(DeclaredFamily):
    """The continuation belonging to the oldest published item."""

    def has_older(self, offset: int, row) -> bool:
        return offset >= row.seq

    def request(self, transcript, row, offset: int) -> bool:
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

    def request(self, transcript, row, offset: int) -> bool:
        if super().has_older(offset, row):
            return False  # Local records remain; the provider is not needed yet.
        return self.request_page(transcript, row)

    def request_page(self, transcript, row) -> bool:
        with transcript.writing() as connection:
            if transcript.oldest(connection) != row:
                return True  # The read retries against the newly published prefix.
            row.continue_with(connection, RequestedEarlierPage(self.cursor, uuid4().hex))
        return True


@dataclass(frozen=True)
class RequestedEarlierPage(EarlierPage):
    """Demand is a transition of the stored continuation, not a second cursor store."""
    request_id: str

    def request_page(self, transcript, row) -> bool:
        return True  # A deferred read keeps the request without another write.

    def frames(self, row) -> tuple:
        return (row,)


class NoItems:
    start = 0

    def has_records(self, through: int) -> bool:
        return False

    def valid_offset(self, offset: int, through: int) -> bool:
        return 0 <= offset <= through

    def has_older(self, offset: int) -> bool:
        return False

    def request_earlier(self, transcript, offset: int) -> bool:
        return False

    def frames(self) -> tuple:
        return ()

    def continue_with(self, connection, earlier: ExternalPage) -> None:
        pass

    def matches(self, item) -> bool:
        return False

    def pending(self, request) -> bool:
        return False


@dataclass(frozen=True)
class ExternalPageRow(TypedRow):
    thread: str = field(metadata={"sql": Column(primary_key=True)})
    turn_id: str = field(metadata={"sql": Column(primary_key=True)})
    item_id: str = field(metadata={"sql": Column(primary_key=True)})
    seq: int
    earlier: ExternalPage = field(default_factory=NoEarlierPage, kw_only=True)

    @property
    def start(self) -> int:
        return self.seq - 1

    def has_records(self, through: int) -> bool:
        return True  # Zero and negative positions name real prepended items.

    def valid_offset(self, offset: int, through: int) -> bool:
        return offset <= through

    def has_older(self, offset: int) -> bool:
        return self.earlier.has_older(offset, self)

    def request_earlier(self, transcript, offset: int) -> bool:
        return self.earlier.request(transcript, self, offset)

    def frames(self) -> tuple:
        return self.earlier.frames(self)

    def matches(self, item) -> bool:
        return (self.turn_id, self.item_id) == (item.turn_id, item.item_id)

    def pending(self, request) -> bool:
        return self.matches(request) and self.earlier == request.request

    def continue_with(self, connection, earlier: ExternalPage) -> None:
        ExternalItemRow.update(connection, where="thread=? AND turn_id=? AND item_id=?",
                               parameters=(self.thread, self.turn_id, self.item_id), earlier=earlier)


@dataclass(frozen=True)
class ExternalItemRow(ExternalPageRow, TypedTable):
    """A stable item position; older pages prepend without moving saved cursors."""

    events: tuple[TranscriptEvent, ...]
    unique = (("thread", "seq"),)

    @classmethod
    def recent_turn_ids(cls, connection: Connection, thread: str) -> Iterator[str]:
        """Yield identities newest first without decoding bodies or grouping the full history."""
        identities: set[str] = set()
        with closing(cls.stream(connection, row_type=ExternalTurn, thread=thread,
                                order_by=("seq",), descending=True)) as rows:
            for row in rows:
                if row.turn_id not in identities:
                    identities.add(row.turn_id)
                    yield row.turn_id


@dataclass(frozen=True)
class ExternalDraftRow(TypedTable, TranscriptDraft):
    """The current CLI item, replaced as it grows and retired at completion."""

    thread: str = field(metadata={"sql": Column(primary_key=True)})
    turn_id: str
    item_id: str
    events: tuple[TranscriptEvent, ...]

    def external_row(self):
        return self

    def end_turn(self, transcript: TypedStore) -> None:
        with transcript.writing() as connection:
            self.delete(connection, thread=self.thread, turn_id=self.turn_id, item_id=self.item_id)


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
class StoreRead(TranscriptSource):
    """One SQL snapshot supplies both empty and populated external reads."""
    transcript: ExternalTranscript
    connection: Connection

    def forward(self, after: int, through: int) -> Iterator[ExternalRecord]:
        return self._records("seq > ? AND seq <= ?", (after, through), descending=False)

    def reverse(self, before: int) -> Iterator[ExternalRecord]:
        return self._records("seq <= ?", (before,), descending=True)

    @property
    def start(self) -> int:
        return self.transcript.oldest(self.connection).start

    def has_records(self, through: int) -> bool:
        return self.transcript.oldest(self.connection).has_records(through)

    def valid_offset(self, offset: int, through: int) -> bool:
        return self.transcript.oldest(self.connection).valid_offset(offset, through)

    def has_older(self, offset: int) -> bool:
        return self.transcript.oldest(self.connection).has_older(offset)

    def request_earlier(self, offset: int) -> bool:
        return self.transcript.oldest(self.connection).request_earlier(self.transcript, offset)

    def pending(self, request) -> bool:
        return self.transcript.oldest(self.connection).pending(request)

    def frames(self) -> tuple:
        return self.transcript.oldest(self.connection).frames()

    def current_draft(self) -> TranscriptDraft:
        return ExternalDraftRow.one(self.connection, thread=self.transcript.lookup) or NoDraft()

    def cut(self) -> ExternalCut:
        return self.transcript._cut(self.connection)

    def _records(self, where: str, parameters: tuple, *,
                 descending: bool) -> Iterator[ExternalRecord]:
        with closing(ExternalItemRow.stream(
            self.connection, where=where, parameters=parameters, order_by=("seq",),
            descending=descending, thread=self.transcript.lookup)) as rows:
            for row in rows:
                yield ExternalRecord(row.seq, row.events)

    def turns(self, bound: TurnBound) -> tuple[TurnDigest, ...]:
        turns: list[TurnDigest] = []
        with closing(ExternalItemRow.recent_turn_ids(self.connection, self.transcript.lookup)) as ids:
            for turn_id in ids:
                turn = TurnDigest(turn_id, tuple(
                    event
                    for row in ExternalItemRow.select(
                        self.connection, thread=self.transcript.lookup, turn_id=turn_id,
                        order_by=("seq",))
                    for event in row.events))
                if not bound.includes(turn):
                    break
                turns.append(turn)
                if bound.full(len(turns)):
                    break
        return tuple(turns)

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
        with self.writing() as connection:
            if events:
                self._insert_once(connection, turn_id, item_id, events, self._cut(connection).offset + 1)
            ExternalDraftRow.delete(connection, thread=self.lookup, turn_id=turn_id, item_id=item_id)

    def _insert_once(self, connection, turn_id, item_id, events, seq) -> bool:
        key = dict(thread=self.lookup, turn_id=turn_id, item_id=item_id)
        if ExternalItemRow.count(connection, **key):
            return False
        ExternalItemRow(seq=seq, events=events, **key).insert(connection)
        return True

    def oldest(self, connection) -> ExternalPageRow | NoItems:
        return ExternalItemRow.one(connection, thread=self.lookup, row_type=ExternalPageRow,
                                   order_by=("seq",), limit=1) or NoItems()

    def publish_page(self, items, earlier: ExternalPage) -> None:
        """Publish the newest snapshot once, including empty items that own a continuation."""
        with self.writing() as connection:
            for item in items:
                self._insert_once(connection, item.turn_id, item.item_id, item.events,
                                  self._cut(connection).offset + 1)
            oldest = self.oldest(connection)
            if items and oldest.matches(items[0]):
                oldest.continue_with(connection, earlier)

    def prepend(self, request, items, earlier: ExternalPage) -> None:
        with self.writing() as connection:
            anchor = self.oldest(connection)
            if not anchor.pending(request):
                return  # An already fulfilled watch frame cannot publish twice.
            anchor.continue_with(connection, NoEarlierPage())
            seq = anchor.seq
            for item in reversed(items):
                if self._insert_once(connection, item.turn_id, item.item_id, item.events, seq - 1):
                    seq -= 1
            self.oldest(connection).continue_with(connection, earlier)

    async def watch_requests(self, emit) -> None:
        previous = ()
        async for _ in WireWatch.observations(*(WatchSource.modified_file(path)
                                               for path in self.source_paths(self.root))):
            with self.reading() as records:
                current = records.frames()
            for frame in current:
                if frame not in previous:
                    emit(frame)
            previous = current

    def draft(self, *, turn_id: str, item_id: str,
              events: tuple[TranscriptEvent, ...]) -> None:
        with self.writing() as connection:
            ExternalDraftRow(self.lookup, turn_id, item_id, events).upsert(connection)

    def end_turn(self) -> None:
        with self.reading() as records:
            draft = records.current_draft()
        draft.end_turn(self)

    @contextmanager
    def reading(self):
        """Keep item and draft queries within the same SQL snapshot."""
        with self.snapshot() as connection:
            yield StoreRead(self, connection)

    def _cut(self, connection) -> ExternalCut:
        return ExternalItemRow.select(connection, row_type=ExternalCut, thread=self.lookup)[0]

