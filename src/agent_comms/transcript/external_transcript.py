"""Transcript items a CLI harness publishes for its sessionless bus thread.

A thread without a native session has no session file of its own; the CLI
bridge publishes each finished item (a reply, a reasoning summary, a tool call,
a compaction) here once. Finished items are append-only, so the thread's page
merges them with its bus messages exactly as a native session's records are
merged: the item's sequence is its position. What the CLI is doing before an
item finishes is the thread's activity, reported through Core's activity log.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import aclosing, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import Annotated, ClassVar

from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.native_runtime_input import NativeRuntimeInput
from agent_comms.execution.transcript_sources import TranscriptSource
from agent_comms.execution.turn_bound import TurnBound
from agent_comms.execution.turn_item import NativeItemId, NativeTurnId
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.watched import FileSource, Watched
from agent_comms.nominal.field_codec import (
    FieldCodec,
    InputId,
    ThreadLookup,
    ToolCallId,
)
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.storage.locked_store import RegistryFile
from agent_comms.storage.recorded_time import EventTime, GeneratedTimestamp
from agent_comms.storage.typed_table import (
    Column,
    DurableStore,
    StoreWriter,
    TypedTable,
)
from agent_comms.transcript.external_transcript_rows import (
    ActiveReplies,
    ExternalCut,
    ExternalEventRow,
    ExternalItemRow,
    ExternalPage,
    ExternalPageRow,
    ExternalRecord,
    ExternalTranscriptRows,
    ExternalTurn,
    ExternalTurnInput,
    NoEarlierPage,
    NoItems,
    ReplyWindow,
)
from agent_comms.transcript.transcript_drafts import TranscriptDraft
from agent_comms.transcript.transcript_events import (
    AssistantTranscript,
    ToolStartTranscript,
    TranscriptEvent,
    TurnDigest,
)
from agent_comms.transport.tool_declaration import ToolDeclaration


@dataclass(frozen=True)
class DraftRow(ExternalTranscriptRows, TypedTable, TranscriptDraft, declared_name="external_draft"):
    """The assigned transcript's current message, shared by native and CLI writers."""

    thread: Annotated[ThreadLookup, Column(primary_key=True)]
    turn_id: NativeTurnId
    item_id: NativeItemId
    events: tuple[TranscriptEvent, ...]

    @classmethod
    @contextmanager
    def publication(cls, root: Path, input_id: InputId) -> Iterator[DraftPublication]:
        from agent_comms.transcript.transcripts import TranscriptRead
        with TranscriptRead.publication_source(root) as connection:
            original = NativeRuntimeInput.one(connection, input_id=input_id) if connection is not None else None
        if original is None:
            yield DraftPublication()
            return
        from agent_comms.registry.registration import Registration
        thread = Registration(RegistryFile.path_in(root)).require_lookup(original.owner_lookup)
        original.owner_identity.require_recorded_owner(thread)
        transcript = ExternalTranscript(root, thread.incarnation)
        with transcript.writer_connection(check_same_thread=False) as connection:
            publication = TurnDraftPublication(
                transcript, connection, FieldCodec.decode(NativeTurnId, input_id))
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

    def tool_call_count(self, since: float) -> int:
        """Count each original call once, including its still-open draft in this snapshot."""
        call = "$." + FieldCodec.record_key(ToolStartTranscript, "tool_call_id")
        stamp = "$." + FieldCodec.record_key(ToolStartTranscript, "timestamp")
        kind = "$." + ToolStartTranscript.family_discriminator
        return self.connection.execute(
            'SELECT count(DISTINCT call_id) FROM ('
            f'SELECT call_id,kind,timestamp FROM "{ExternalEventRow.declared_name}" WHERE thread=? '
            'UNION ALL '
            'SELECT json_extract(item.value,?),json_extract(item.value,?),json_extract(item.value,?) '
            f'FROM "{DraftRow.declared_name}" AS draft,json_each(draft.events) AS item '
            'WHERE draft.thread=?) WHERE kind=? AND timestamp>=?',
            (self.transcript.lookup, call, kind, stamp, self.transcript.lookup,
             FieldCodec.encode(ToolStartTranscript), since),
        ).fetchone()[0]

    def unjudged(self, todo, since):
        where, parameters = ExternalEventRow.unjudged_query(self.transcript.lookup, todo, since)
        with closing(self.connection.execute(
            'SELECT timestamp,call_id,json_extract(event,\'$.tool\') FROM '
            '(SELECT e.event, e.timestamp, e.call_id, e.seq, e.ordinal, '
            'row_number() OVER (PARTITION BY e.call_id ORDER BY e.seq DESC,e.ordinal DESC) AS latest '
            f'FROM "{ExternalEventRow.declared_name}" e WHERE {where}) '
            'WHERE latest=1 ORDER BY seq DESC,ordinal DESC', parameters)) as rows:
            for timestamp, call_id, encoded in rows:
                identity = FieldCodec.decode(ToolCallId, call_id)
                tool = FieldCodec.decode_bound(ToolDeclaration, FieldCodec.parse(encoded),
                                               call_id=identity)
                yield from tool.edit_calls(identity, GeneratedTimestamp.decode(timestamp),
                                           todo.held().owner, todo.id)


@dataclass(frozen=True, slots=True)
class ExternalTranscript(TranscriptSource, DurableStore):
    """One sessionless thread's CLI items: published by its bridge, paged by Core."""

    root: Path
    incarnation: ThreadIncarnation
    filename: ClassVar[str] = "external_transcripts.sqlite3"
    tables = ExternalTranscriptRows
    # The CLI keeps its own items; a lost tail is republished when a watch starts.
    synchronous: ClassVar[str] = "NORMAL"

    @property
    def lookup(self) -> ThreadLookup:
        return self.incarnation.require_recorded().lookup

    def last_tool_call(self) -> EventTime:
        with self.snapshot() as connection:
            timestamp = connection.execute(
                f'SELECT (SELECT timestamp FROM "{ExternalEventRow.declared_name}" '
                'WHERE thread=? AND kind=? ORDER BY seq DESC,ordinal DESC LIMIT 1)',
                (self.lookup, FieldCodec.encode(ToolStartTranscript)),
            ).fetchone()[0]
        return GeneratedTimestamp.decode(timestamp)

    def delivered(self, turn_id: NativeTurnId, references: tuple[MessageReference, ...]) -> None:
        if not references:
            return  # Protocol notices and already-confirmed history introduce no input.
        with self.writing() as connection:
            turn = ExternalTurn.opened(connection, self.lookup, turn_id)
            turn.replies.delivered(connection, turn, references)

    def opening_for(self, turn_id: NativeTurnId) -> tuple[MessageReference, ...]:
        with self.snapshot() as connection:
            turns = ExternalTurn.select(connection, thread=self.lookup, turn_id=turn_id)
            return next((turn.replies.references_in(connection, turn) for turn in turns), ())

    def answered_inputs(self, references: tuple[MessageReference, ...]) -> frozenset[MessageReference]:
        """Probe only named originals and the final items of the turns they opened."""
        with self.snapshot() as connection:
            inputs = ExternalTurnInput.select_keys(connection, tuple(
                dict(thread=self.lookup, reference=reference) for reference in references))
            turns = tuple(dict.fromkeys(row.turn_id for row in inputs))
            answered = frozenset(reference
                for turn_id in turns
                for turn in ExternalTurn.select(connection, thread=self.lookup, turn_id=turn_id)
                for reference in turn.replies.answered(connection, turn))
        return answered.intersection(references)

    def finish_replies(self, turn_id: NativeTurnId) -> None:
        with self.writing() as connection:
            for turn in ExternalTurn.select(connection, thread=self.lookup, turn_id=turn_id):
                turn.store(connection, turn.replies.finished())

    def transition_replies(self, transition: Callable[[ReplyWindow], ReplyWindow]) -> None:
        """Indexed active-state probes retire all open turns without decoding history."""
        with self.writing() as connection:
            for turn in ExternalTurn.select_keys(connection, tuple(
                dict(thread=self.lookup, replies=member())
                for member in ReplyWindow.members_with(ActiveReplies))):
                turn.store(connection, transition(turn.replies))

    def observe_output(self, connection: Connection, turn_id: NativeTurnId,
                       events: tuple[TranscriptEvent, ...]) -> None:
        turn = ExternalTurn.opened(connection, self.lookup, turn_id)
        turn.store(connection, turn.replies.observed(events))

    def finish(self, *, turn_id: NativeTurnId, item_id: NativeItemId,
               events: tuple[TranscriptEvent, ...]) -> None:
        """Append a finished item once; an item with no presentation adds nothing."""
        with self.writing() as connection:
            self.observe_output(connection, turn_id, events)
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
        rows = ExternalItemRow.select(connection, thread=self.lookup, row_type=ExternalPageRow,
                                      order_by=("seq",), limit=1)
        return next(iter(rows), NoItems())

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
        async with aclosing(Watched.watch_sources(
            *(FileSource(path) for path in self.source_paths(self.root))
        )) as observations:
            async for _ in observations:
                current = await Coordination.run_worker(self.requested_frames)
                for frame in current:
                    if frame not in previous:
                        emit(frame)
                previous = current

    def requested_frames(self) -> tuple:
        with self.reading() as records:
            return records.frames()

    def draft(self, *, turn_id: NativeTurnId, item_id: NativeItemId,
              events: tuple[TranscriptEvent, ...]) -> None:
        with self.writing() as connection:
            self.observe_output(connection, turn_id, events)
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


@dataclass(eq=False, repr=False)
class TurnDraftPublication(DraftPublication, StoreWriter):
    """A native turn owns one connection; each message owns one draft identity."""

    transcript: ExternalTranscript
    connection: Connection
    turn_id: NativeTurnId

    @property
    def store(self) -> ExternalTranscript:
        return self.transcript

    @contextmanager
    def writing(self):
        with self.transaction(self.connection):
            yield self.connection

    def begin_message(self, timestamp: float) -> None:
        self.item_id = NativeItemId.fresh()
        self.timestamp = timestamp

    def publish(self, parts: Iterable[str]) -> None:
        DraftRow(self.transcript.lookup, self.turn_id, self.item_id,
                 (AssistantTranscript(''.join(parts), position=self.item_id,
                                      timestamp=self.timestamp),)).publish(self)

    def retire_message(self) -> None:
        with self.writing() as connection:
            DraftRow.delete(connection, thread=self.transcript.lookup, turn_id=self.turn_id)
