"""Transcript items a CLI harness publishes for its sessionless bus thread.

A thread without a native session has no session file of its own; the CLI
bridge publishes each finished item (a reply, a reasoning summary, a tool call,
a compaction) here once. Finished items are append-only, so the thread's page
merges them with its bus messages exactly as a native session's records are
merged: the item's sequence is its position. What the CLI is doing before an
item finishes is the thread's activity, reported through Core's activity log.
"""

from __future__ import annotations

from collections.abc import Generator, Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from sqlite3 import Connection
from typing import TYPE_CHECKING, Annotated, ClassVar
from uuid import uuid4

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .locked_store import RegistryFile
from .native_transcript import RecordsCut
from .recorded_time import EventTime, GeneratedTimestamp
from .thread_identity import ThreadIncarnation
from .tool_declaration import ToolDeclaration
from .transcript_events import ToolStartTranscript, TranscriptEvent, TurnDigest
from .transcript_sources import TranscriptDraft, TranscriptRecord, TranscriptSource
from .typed_table import (
    Column,
    DurableTables,
    ForeignKey,
    Generated,
    Index,
    RealStorage,
    StoreWriter,
    TypedRow,
    TypedStore,
    TypedTable,
)
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
    thread: Annotated[str, Column(primary_key=True)]
    turn_id: Annotated[str, Column(primary_key=True)]
    item_id: Annotated[str, Column(primary_key=True)]
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

    thread: Annotated[str, Column(primary_key=True)]
    seq: Annotated[int, Column(primary_key=True)]
    ordinal: Annotated[int, Column(primary_key=True)]
    event: TranscriptEvent
    timestamp: ClassVar[Generated] = Generated(
        EventTime,
        Column(
            storage=RealStorage, nullable=True, generated="json_extract({event}, '$.timestamp')"
        ),
    )
    kind: ClassVar[Generated] = Generated(str, Column(generated="json_extract({event}, '$.kind')"))
    tool: ClassVar[Generated] = Generated(
        str,
        Column(generated="json_extract({event}, '$.tool.tool')", nullable=True),
    )
    call_id: ClassVar[Generated] = Generated(
        str,
        Column(generated="json_extract({event}, '$.tool_call_id')", nullable=True),
    )

    foreign_keys = (
        ForeignKey(("thread", "seq"), ExternalItemRow, ("thread", "seq"), on_delete="CASCADE"),
    )
    indexes = (
        Index(("thread", "kind", "tool", "timestamp", "call_id")),
        Index(("thread", "kind", "timestamp")),
        Index(("thread", "kind", "seq", "ordinal")),
    )

    @classmethod
    def append(cls, connection, thread, seq, events) -> None:
        for ordinal, event in enumerate(events):
            cls(thread, seq, ordinal, event).insert(connection)

    @classmethod
    def unjudged_query(cls, thread, todo):
        from .native_tools import MutatingPathTool
        from .tool_declaration import ToolDeclaration
        from .transcript_events import ToolStartTranscript

        tools = tuple(tool.declared_name for tool in ToolDeclaration.members_with(MutatingPathTool))
        judged = tuple(verdict.tool_call_id for verdict in todo.verdicts)
        placeholders = lambda values: ",".join("?" for _ in values)
        since = todo.order.at.recorded_at("An order")
        where = (f'e.thread=? AND e.kind=? AND e.tool IN ({placeholders(tools)}) '
                 'AND (e.timestamp>=? OR e.timestamp IS NULL)')
        parameters = (thread, ToolStartTranscript.declared_name, *tools, since)
        if judged:
            where += f' AND e.call_id NOT IN ({placeholders(judged)})'
            parameters += judged
        return where, parameters


@dataclass(frozen=True)
class DraftRow(TypedTable, TranscriptDraft, declared_name="external_draft"):
    """The assigned transcript's current message, shared by native and CLI writers."""

    thread: Annotated[str, Column(primary_key=True)]
    turn_id: str
    item_id: str
    events: tuple[TranscriptEvent, ...]

    def external_row(self):
        return self

    @classmethod
    @contextmanager
    def publication(cls, root: Path, input_id: str):
        from .native_runtime_input import NativeRuntimeInput
        from .transcripts import TranscriptRead
        with TranscriptRead.publication_source(root) as connection:
            original = NativeRuntimeInput.one(connection, input_id=input_id) if connection is not None else None
        if original is None:
            yield DraftPublication()
            return
        from .registration import Registration
        thread = Registration(RegistryFile.path_in(root)).require(original.owner_thread)
        original.require_recorded_owner(thread)
        transcript = ExternalTranscript(root, thread.incarnation)
        with transcript.writer_connection(check_same_thread=False) as connection:
            publication = TurnDraftPublication(transcript, connection, input_id)
            try:
                yield publication
            finally:
                publication.retire_message()

    def publish(self, transcript: StoreWriter) -> None:
        with transcript.writing() as connection:
            self.upsert(connection)

    def end_turn(self, transcript: StoreWriter) -> None:
        with transcript.writing() as connection:
            self.delete(connection, thread=self.thread, turn_id=self.turn_id, item_id=self.item_id)


@dataclass(frozen=True)
class ExternalRecord(TranscriptRecord):
    """An item at its page position, with events but no decoded native entry."""

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


@dataclass(frozen=True)
class ExternalCut(RecordsCut, TypedRow):
    """The thread's newest finished item sequence when read."""

    offset: Annotated[int, Column(generated="coalesce(max({seq}), 0)")]

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

    @contextmanager
    def draft_snapshot(self, root: Path, incarnation: ThreadIncarnation):
        yield self.connection

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
            'SELECT timestamp,call_id,json_extract(event,\'$.tool\') FROM '
            '(SELECT e.event, e.timestamp, e.call_id, e.seq, e.ordinal, '
            'row_number() OVER (PARTITION BY e.call_id ORDER BY e.seq DESC,e.ordinal DESC) AS latest '
            f'FROM "{ExternalEventRow.declared_name}" e WHERE {where}) '
            'WHERE latest=1 ORDER BY seq DESC,ordinal DESC', parameters)) as rows:
            for timestamp, call_id, encoded in rows:
                identity = FieldCodec.decode(str, call_id)
                tool = FieldCodec.decode_bound(ToolDeclaration, FieldCodec.parse(encoded),
                                               call_id=identity)
                yield from tool.edit_calls(identity, GeneratedTimestamp.decode(timestamp),
                                           todo.held().owner, todo.id)

    def unjudged_count(self, todo) -> int:
        where, parameters = ExternalEventRow.unjudged_query(self.transcript.lookup, todo)
        return self.connection.execute(
            f'SELECT count(DISTINCT e.call_id) FROM "{ExternalEventRow.declared_name}" e WHERE {where}',
            parameters).fetchone()[0]


@dataclass(frozen=True, slots=True)
class ExternalTranscript(TranscriptSource, TypedStore, DurableTables):
    """One sessionless thread's CLI items: published by its bridge, paged by Core."""

    root: Path
    incarnation: ThreadIncarnation
    filename: ClassVar[str] = "external_transcripts.sqlite3"
    rows: ClassVar[tuple[type[TypedTable], ...]] = (ExternalItemRow, ExternalEventRow, DraftRow)
    # The CLI keeps its own items; a lost tail is republished when a watch starts.
    synchronous: ClassVar[str] = "NORMAL"

    @property
    def lookup(self) -> str:
        return self.incarnation.require_recorded().lookup

    def tool_call_between(self, since: float, through: float) -> bool:
        """Original tool timestamps decide first-call timeliness, not the last watcher tick."""
        with self.snapshot() as connection:
            return bool(connection.execute(
                f'SELECT EXISTS(SELECT 1 FROM "{ExternalEventRow.declared_name}" '
                'WHERE thread=? AND kind=? AND timestamp>=? AND timestamp<=?)',
                (self.lookup, FieldCodec.encode(ToolStartTranscript), since, through),
            ).fetchone()[0])

    def last_tool_call(self) -> EventTime:
        with self.snapshot() as connection:
            timestamp = connection.execute(
                f'SELECT (SELECT timestamp FROM "{ExternalEventRow.declared_name}" '
                'WHERE thread=? AND kind=? ORDER BY seq DESC,ordinal DESC LIMIT 1)',
                (self.lookup, FieldCodec.encode(ToolStartTranscript)),
            ).fetchone()[0]
        return GeneratedTimestamp.decode(timestamp)

    def finish(self, *, turn_id: str, item_id: str,
               events: tuple[TranscriptEvent, ...]) -> None:
        """Append a finished item once; an item with no presentation adds nothing."""
        with self.writing() as connection:
            if events:
                self._insert_once(connection, turn_id, item_id, events, self._cut(connection).offset + 1)
            DraftRow.delete(connection, thread=self.lookup, turn_id=turn_id, item_id=item_id)

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
        with self.writing() as connection:
            DraftRow(self.lookup, turn_id, item_id, events).upsert(connection)

    def end_turn(self) -> None:
        with self.reading() as records:
            draft = records.current_draft(self.root, self.incarnation)
        draft.end_turn(self)

    def turns(self, bound: TurnBound) -> tuple[TurnDigest, ...]:
        """The stored CLI turn IDs, not native input entries, own these boundaries."""
        with self.reading() as reader:
            return reader.turns(bound)

    @contextmanager
    def reading(self) -> Iterator[StoreRead]:
        """Keep item and draft queries within the same SQL snapshot."""
        with self.snapshot() as connection:
            yield StoreRead(self, connection)

    def _cut(self, connection) -> ExternalCut:
        return ExternalItemRow.select(connection, row_type=ExternalCut, thread=self.lookup)[0]


class DraftPublication:
    """Unassigned native turns have no assigned draft to publish."""

    def begin_message(self, timestamp: float) -> None:
        pass

    def publish(self, parts: Iterable[str]) -> None:
        pass

    def retire_message(self) -> None:
        pass


class TurnDraftPublication(DraftPublication, StoreWriter):
    """A native turn owns one connection; each message owns one draft identity."""

    def __init__(self, transcript: ExternalTranscript, connection: Connection, turn_id: str):
        self.transcript = transcript
        self.connection = connection
        self.turn_id = turn_id

    @property
    def store(self) -> ExternalTranscript:
        return self.transcript

    @contextmanager
    def writing(self):
        with self.transaction(self.connection):
            yield self.connection

    def begin_message(self, timestamp: float) -> None:
        self.item_id = uuid4().hex
        self.timestamp = timestamp

    def publish(self, parts: Iterable[str]) -> None:
        from .transcript_events import AssistantTranscript
        DraftRow(self.transcript.lookup, self.turn_id, self.item_id,
                 (AssistantTranscript(''.join(parts), position=self.item_id,
                                      timestamp=self.timestamp),)).publish(self)

    def retire_message(self) -> None:
        with self.writing() as connection:
            DraftRow.delete(connection, thread=self.transcript.lookup, turn_id=self.turn_id)
