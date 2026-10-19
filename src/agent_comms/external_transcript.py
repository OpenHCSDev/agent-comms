"""Transcript items a CLI harness publishes for its sessionless bus thread.

A thread without a native session has no session file of its own; the CLI
bridge publishes each finished item (a reply, a reasoning summary, a tool call,
a compaction) here once. Finished items are append-only, so the thread's page
merges them with its bus messages exactly as a native session's records are
merged: the item's sequence is its position. What the CLI is doing before an
item finishes is the thread's activity, reported through Core's activity log.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import closing, contextmanager
from sqlite3 import Connection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, TYPE_CHECKING, ClassVar
from uuid import uuid4

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .recorded_time import EventTime, GeneratedTimestamp, UnrecordedEventTime
from .native_transcript import RecordsCut
from .thread_identity import ThreadIncarnation
from .transcript_events import TranscriptEvent, TurnDigest
from .transcript_sources import NoDraft, TranscriptDraft, TranscriptSource
from .typed_table import Column, ForeignKey, Index, RealStorage, TypedRow, TypedStore, TypedTable
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

    unique = (("thread", "seq"),)
    indexes = (Index(("thread", "turn_id", "seq")),)

    def events_in(self, connection) -> tuple[TranscriptEvent, ...]:
        return tuple(row.event for row in ExternalEventRow.select(
            connection, thread=self.thread, seq=self.seq, order_by=("ordinal",)))

    def record(self, connection) -> ExternalRecord:
        return ExternalRecord(self.seq, self.events_in(connection))

    @classmethod
    def recent_turn_ids(cls, connection: Connection, thread: str) -> Generator[str]:
        """Yield identities newest first without decoding bodies or grouping the full history."""
        identities: set[str] = set()
        with closing(cls.stream(connection, row_type=ExternalTurn, thread=thread,
                                order_by=("seq",), descending=True)) as rows:
            for row in rows:
                if row.turn_id not in identities:
                    identities.add(row.turn_id)
                    yield row.turn_id


@dataclass(frozen=True)
class ExternalEventRow(TypedTable):
    """Each saved event exists once; SQL derives its query keys from that event."""

    thread: str = field(metadata={"sql": Column(primary_key=True)})
    seq: int = field(metadata={"sql": Column(primary_key=True)})
    ordinal: int = field(metadata={"sql": Column(primary_key=True)})
    event: TranscriptEvent
    timestamp: Annotated[EventTime, GeneratedTimestamp] = field(metadata={"sql": Column(
        storage=RealStorage, nullable=True, generated="json_extract({event}, '$.timestamp')")})
    kind: str = field(metadata={"sql": Column(generated="json_extract({event}, '$.kind')")})
    tool: str | None = field(metadata={"sql": Column(generated="json_extract({event}, '$.tool.tool')")})
    call_id: str | None = field(metadata={"sql": Column(generated="json_extract({event}, '$.tool_call_id')")})

    foreign_keys = (ForeignKey(("thread", "seq"), ExternalItemRow, ("thread", "seq"), on_delete="CASCADE"),)
    indexes = (Index(("thread", "kind", "tool", "timestamp", "call_id")),)

    @classmethod
    def append(cls, connection, thread, seq, events) -> None:
        for ordinal, event in enumerate(events):
            cls(thread, seq, ordinal, event, UnrecordedEventTime(), "", None, None).insert(connection)

    @classmethod
    def unjudged_query(cls, thread, todo):
        from .native_tools import MutatingPathTool
        from .tool_declaration import ToolDeclaration
        from .transcript_events import ToolStartTranscript

        tools = tuple(tool.declared_name for tool in ToolDeclaration.members_with(MutatingPathTool))
        judged = tuple(verdict.tool_call_id for verdict in todo.verdicts)
        placeholders = lambda values: ",".join("?" for _ in values)
        since = todo.order.at.recorded_at("An order")
        # SQL NULL is the original undated fact, decoded through FieldCodec
        # as UnrecordedEventTime when a row is read. No time is substituted.
        table = cls.declared_name
        selected = f'u.thread=? AND u.kind=? AND u.tool IN ({placeholders(tools)})'
        # Two ranges keep timestamp in the index search, rather than scanning
        # every historical edit to evaluate an OR for each row.
        where = (
            f'e.rowid IN (SELECT u.rowid FROM "{table}" u WHERE {selected} AND u.timestamp>=? '
            f'UNION ALL SELECT u.rowid FROM "{table}" u WHERE {selected} '
            'AND u.timestamp IS NULL)'
        )
        scope = (thread, ToolStartTranscript.declared_name, *tools)
        parameters = (*scope, since, *scope)
        if judged:
            where += f' AND e.call_id NOT IN ({placeholders(judged)})'
            parameters += judged
        return where, parameters


@dataclass(frozen=True)
class DraftRow(TypedTable, TranscriptDraft):
    """The assigned transcript's current message, shared by native and CLI writers."""

    thread: str = field(metadata={"sql": Column(primary_key=True)})
    turn_id: str
    item_id: str
    events: tuple[TranscriptEvent, ...]

    def external_row(self):
        return self

    @classmethod
    def current(cls, root: Path, thread: str) -> TranscriptDraft:
        from .coordination_database import CoordinationStore
        with CoordinationStore.observed(root / 'coordination.sqlite3').observing(cls, thread) as draft:
            return draft

    @classmethod
    def capture(cls, connection, thread):
        return (cls.one(connection, thread=thread) if cls.present(connection) else None) or NoDraft()

    @classmethod
    def missing_store(cls, store):
        return NoDraft()

    @classmethod
    def invalid_store(cls, store):
        raise ValueError('Draft source is not a valid coordinator')

    @classmethod
    def for_input(cls, root: Path, input_id: str) -> TranscriptDraft:
        from .native_runtime_input import NativeRuntimeInput
        from .transcripts import TranscriptRead
        with TranscriptRead.publication_source(root) as connection:
            original = NativeRuntimeInput.one(connection, input_id=input_id) if connection is not None else None
        return (cls(original.owner_lookup, input_id, uuid4().hex, ())
                if original is not None else NoDraft())

    def publish(self, root: Path, events: tuple[TranscriptEvent, ...]) -> None:
        from dataclasses import replace
        from .coordination_database import CoordinationStore
        with closing(CoordinationStore.writer(root / 'coordination.sqlite3')) as connection:
            with connection:
                self.create(connection, if_not_exists=True)
                replace(self, events=events).upsert(connection)

    def publish_text(self, root: Path, text: str, timestamp: float) -> None:
        from .transcript_events import AssistantTranscript
        self.publish(root, (AssistantTranscript(text, timestamp=timestamp, position=self.item_id),))

    def end_turn(self, root: Path) -> None:
        from .coordination_database import CoordinationStore
        if not (root / 'coordination.sqlite3').exists():
            return
        with closing(CoordinationStore.writer(root / 'coordination.sqlite3')) as connection:
            with connection:
                if self.present(connection):
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

    def cut(self) -> ExternalCut:
        return self.transcript._cut(self.connection)

    def _records(self, where: str, parameters: tuple, *,
                 descending: bool) -> Iterator[ExternalRecord]:
        with closing(ExternalItemRow.stream(
            self.connection, where=where, parameters=parameters, order_by=("seq",),
            descending=descending, thread=self.transcript.lookup)) as rows:
            for row in rows:
                yield row.record(self.connection)

    def turns(self, bound: TurnBound) -> tuple[TurnDigest, ...]:
        with closing(ExternalItemRow.recent_turn_ids(self.connection, self.transcript.lookup)) as ids:
            return bound.take(
                TurnDigest(turn_id, tuple(
                    event
                    for row in ExternalItemRow.select(
                        self.connection, thread=self.transcript.lookup, turn_id=turn_id,
                        order_by=("seq",))
                    for event in row.events_in(self.connection)))
                for turn_id in ids)

    def unjudged(self, todo):
        where, parameters = ExternalEventRow.unjudged_query(self.transcript.lookup, todo)
        with closing(self.connection.execute(
            'SELECT event FROM (SELECT e.event, e.seq, e.ordinal, '
            'row_number() OVER (PARTITION BY e.call_id ORDER BY e.seq DESC,e.ordinal DESC) AS latest '
            f'FROM "{ExternalEventRow.declared_name}" e WHERE {where}) '
            'WHERE latest=1 ORDER BY seq DESC,ordinal DESC', parameters)) as rows:
            for encoded, in rows:
                event = FieldCodec.decode(TranscriptEvent, FieldCodec.parse(encoded))
                yield from event.edit_calls(todo.held().owner, todo.id,
                                            todo.order.at.recorded_at("An order"))

    def unjudged_count(self, todo) -> int:
        where, parameters = ExternalEventRow.unjudged_query(self.transcript.lookup, todo)
        return self.connection.execute(
            f'SELECT count(DISTINCT e.call_id) FROM "{ExternalEventRow.declared_name}" e WHERE {where}',
            parameters).fetchone()[0]

@dataclass(frozen=True, slots=True)
class ExternalTranscript(TranscriptSource, TypedStore):
    """One sessionless thread's CLI items: published by its bridge, paged by Core."""

    root: Path
    incarnation: ThreadIncarnation
    filename: ClassVar[str] = "external_transcripts.sqlite3"
    rows: ClassVar[tuple[type[TypedTable], ...]] = (ExternalItemRow, ExternalEventRow)
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
        DraftRow(self.lookup, turn_id, item_id, ()).end_turn(self.root)

    def _insert_once(self, connection, turn_id, item_id, events, seq) -> bool:
        key = dict(thread=self.lookup, turn_id=turn_id, item_id=item_id)
        if ExternalItemRow.count(connection, **key):
            return False
        ExternalItemRow(seq=seq, **key).insert(connection)
        ExternalEventRow.append(connection, self.lookup, seq, events)
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
        DraftRow(self.lookup, turn_id, item_id, ()).publish(self.root, events)

    def end_turn(self) -> None:
        self.current_draft(self.root, self.lookup).end_turn(self.root)

    @contextmanager
    def reading(self):
        """Keep item and draft queries within the same SQL snapshot."""
        with self.snapshot() as connection:
            yield StoreRead(self, connection)

    def _cut(self, connection) -> ExternalCut:
        return ExternalItemRow.select(connection, row_type=ExternalCut, thread=self.lookup)[0]

