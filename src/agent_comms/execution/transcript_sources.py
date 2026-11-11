"""The canonical wire owns incoming and outgoing conversation history.

Native bytes and committed wire sequences are independent source coordinates.
A display cursor advances each only when that source's record is consumed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Awaitable, Callable, Generator, Iterable, Iterator, Sequence
from contextlib import aclosing, closing, contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from itertools import takewhile
from math import inf
from pathlib import Path
from sqlite3 import Connection
from typing import Annotated, ClassVar, Self

from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.errors import StaleRevision, TranscriptChanged
from agent_comms.errors import RelationViolationError
from agent_comms.execution.context_entry_reader import ReadsContextEntries
from agent_comms.execution.history_view import HistoryView
from agent_comms.execution.native_entries import (
    MessageEntry,
    NativeEntry,
    RootEntry,
    SessionEntry,
    TranscriptProjection,
)
from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.native_input_proof import (
    NativeContextJournal,
    NativeContextProof,
    NativeInputLocation,
)
from agent_comms.execution.native_output import NativeRecordLocation
from agent_comms.execution.native_record_file import (
    MissingSessionCut,
    PresentSessionCut,
    ReadsNativeInputs,
    RecordsCut,
    SessionCut,
    record_end,
    reverse_records,
)
from agent_comms.execution.native_records import (
    NativeRecord,
    NoNativeInput,
    TranscriptRecord,
)
from agent_comms.execution.native_runtime_input import (
    NativeRuntimeInput,
    PublishedReplyRevision,
)
from agent_comms.execution.native_session_files import NativeSessionFiles
from agent_comms.execution.native_session_reopen import NativeSessionIdentity
from agent_comms.execution.transcript_cursor_reads import (
    MarksSourceRead,
    TranscriptRuntime,
)
from agent_comms.execution.transcript_routes import TranscriptRoutes, _SessionRoutes
from agent_comms.execution.turn_bound import TurnBound
from agent_comms.execution.turn_context import SegmentManifest
from agent_comms.execution.turn_item import FreshNativeId, NativeItemId, NativeTurnId
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.bus_publication import Delivered
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import Message
from agent_comms.messaging.published_messages import ReadsPublishedMessages
from agent_comms.messaging.sent_message import (
    AssignedSourceIdentity,
    PublishedSentMessage,
)
from agent_comms.messaging.sent_reports import ResolvesSentReports
from agent_comms.messaging.watched import FileSource, Watched
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    FieldCodec,
    InputId,
    MalformedRecord,
    ThreadLookup,
    ToolCallId,
)
from agent_comms.registry.thread_identity import (
    RecordedThreadIncarnation,
    ThreadIncarnation,
)
from agent_comms.registry.thread_incarnation_read import ReadsThreadIncarnation
from agent_comms.storage.locked_store import RegistryFile
from agent_comms.storage.private_path import FileIdentity, FileRevision, MissingFile
from agent_comms.storage.recorded_time import (
    EventTime,
    GeneratedTimestamp,
    RecordedEventTime,
    UnrecordedEventTime,
)
from agent_comms.storage.typed_table import (
    Column,
    DurableStore,
    StoreWriter,
    TypedTable,
)
from agent_comms.team.rule_deal import Mute, Speaking, Voice
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
from agent_comms.transcript.transcript_drafts import NoDraft, TranscriptDraft
from agent_comms.transcript.transcript_events import (
    AssistantTranscript,
    IncomingTranscript,
    SentTranscript,
    TextTranscript,
    ToolStartTranscript,
    TranscriptEvent,
    TurnDigest,
)
from agent_comms.transport.tool_declaration import ToolDeclaration


class WireFrontier(DeclaredFamily, ResolvesSentReports, affix="WireFrontier"):
    """The wire coordinate owns cursor construction, bounds and source authority."""

    sequence: int

    def cursor(self, session_file: str, offset: int, *,
               file: FileIdentity | MissingFile) -> NativeTranscriptCursor:
        return NativeTranscriptCursor(session_file, offset, file=file)

    def at(self, sequence: int) -> Self:
        if sequence:
            raise MalformedRecord("Native-only cursor cannot advance an assigned wire source")
        return self

    def within(self, parent: WireFrontier) -> bool:
        return True

    def contains(self, other: AssignedSourceCursor) -> bool:
        return False

    def require_source(self, identity: AssignedSourceIdentity) -> None:
        """An unassigned position does not claim a particular wire source."""

    def require_native(self) -> None:
        """An unassigned position carries only the native coordinate."""

    def resolve_sent(
        self, messages: tuple[SentTranscript, ...],
    ) -> tuple[SentTranscript | PublishedSentMessage, ...]:
        return tuple(messages)


@dataclass(frozen=True)
class NoWireFrontier(WireFrontier):
    sequence = 0


@dataclass(frozen=True)
class AssignedSourceCursor(AssignedSourceIdentity, WireFrontier):
    sequence: int = field()

    def cursor(self, session_file: str, offset: int, *,
               file: FileIdentity | MissingFile) -> NativeTranscriptCursor:
        return WireTranscriptCursor(session_file, offset, self, file=file)

    def within(self, parent: WireFrontier) -> bool:
        return parent.contains(self)

    def contains(self, other: AssignedSourceCursor) -> bool:
        return self.same_publication_source(other) and other.sequence <= self.sequence

    def require_native(self) -> None:
        raise TranscriptChanged()

    def resolve_sent(
        self, messages: tuple[SentTranscript, ...],
    ) -> tuple[SentTranscript | PublishedSentMessage, ...]:
        return AssignedTranscriptSource.open(self.identity).resolve_sent(messages)

    @property
    def identity(self) -> AssignedSourceIdentity:
        return AssignedSourceIdentity(self.root, self.recipient)

    def require_source(self, identity: AssignedSourceIdentity) -> None:
        if not self.same_publication_source(identity):
            raise TranscriptChanged()

    def at(self, sequence: int) -> Self:
        return replace(self, sequence=sequence)


@dataclass(frozen=True)
class AssignedSourceWindow:
    """Conversation and sent sources captured with one acknowledgement frontier."""

    cursor: WireFrontier
    sources: tuple[Delivered, ...]
    sent_sources: tuple[Delivered, ...]


class TranscriptTraversal(ABC):
    @abstractmethod
    def native(self, reader: TranscriptSource, cursor: TranscriptCursor, through: TranscriptCursor) -> Generator[TranscriptRecord, None, None]: ...

    @abstractmethod
    def source_rows(
        self,
        source: AssignedTranscriptSource,
        sequence: int,
        through: TranscriptCursor,
        limit: int,
    ) -> tuple[Delivered, ...]: ...

    @abstractmethod
    def chooses_native(self, native_time: float | None, receipt_time: float) -> bool: ...

    @abstractmethod
    def native_position(self, record: TranscriptRecord) -> int: ...

    @abstractmethod
    def receipt_position(self, message: Delivered) -> int: ...

    @abstractmethod
    def finish(self, records: list[tuple[TranscriptEvent, ...]]) -> tuple[TranscriptEvent, ...]: ...

    @abstractmethod
    def bounds(
        self, initial: TranscriptCursor, consumed: TranscriptCursor,
    ) -> tuple[TranscriptCursor, TranscriptCursor]: ...

    def round_edges(self, batch: tuple[TranscriptEvent, ...]) -> tuple[set[str], set[str]]:
        """The tool-call ids a batch opens and closes, by traversal direction.

        A saved tool round is its call record together with its result record;
        a page window never stops between them.
        """
        return self.oriented(
            {call for event in batch for call in event.started_calls()},
            {call for event in batch for call in event.ended_calls()},
        )

    def oriented(self, started: set[str], ended: set[str]) -> tuple[set[str], set[str]]:
        """Reading newest-first meets a round's result before its call."""
        return ended, started

    def exhausted_offset(self, frontier: TranscriptCursor, reader: TranscriptSource) -> int:
        return reader.start

    def exhausted_sequence(self, frontier: TranscriptCursor) -> int:
        return 0

    def read_records(
        self,
        reader: TranscriptSource,
        project,
        sources: TranscriptSource,
        initial: TranscriptCursor,
        frontier: TranscriptCursor,
        view: HistoryView,
        *,
        max_messages: int,
        max_bytes: int,
    ) -> tuple[TranscriptCursor, list[tuple[TranscriptEvent, ...]]]:
        """Advance each file position only after reading the record at that position. A record or
        message the view does not keep is passed over like an eventless record, so it neither
        fills the page nor stops it."""
        consumed, records, used = initial, [], 0
        # A tool round opened by an already appended record keeps the window open
        # until its paired record is appended too; the bounded grace keeps an
        # interrupted call (its result record never arrives) from growing a page.
        open_rounds: set[str] = set()
        round_grace = 0
        native = (self.native(reader, initial, frontier) if reader.has_records(frontier.offset)
                  else (record for record in ()))
        projected = ((record, view.kept(events)) for record, events
                     in project(native, max_records=max_messages + 1, max_bytes=max_bytes))
        try:
            record, events = next(projected, (None, ()))
            source_rows = iter(
                sources.page_rows(
                    self, consumed.wire.sequence, frontier, limit=max_messages + 1
                )
            )
            message = next(source_rows, None)
            while record is not None or message is not None:
                if record is not None and not events:
                    consumed = consumed.at_offset(self.native_position(record))
                    record, events = next(projected, (None, ()))
                    continue
                # NativeEntry owns one clock for all parts of a record.
                native_time = events[0].timestamp if events else None
                choose_native = record is not None and (
                    message is None
                    or self.chooses_native(native_time, message.message.timestamp)
                )
                batch = events if choose_native else view.kept(sources.events(message))
                if not batch:
                    consumed = consumed.at_sequence(self.receipt_position(message.message))
                    message = next(source_rows, None)
                    continue
                cost = record.size if choose_native else 0
                if records and (len(records) >= max_messages or (choose_native and used + cost > max_bytes)):
                    if not open_rounds or round_grace >= self.round_grace_limit:
                        break
                    round_grace += 1
                records.append(batch)
                opened, closed = self.round_edges(batch)
                open_rounds |= opened
                open_rounds -= closed
                used += cost
                if choose_native:
                    consumed = consumed.at_offset(self.native_position(record))
                    record, events = next(projected, (None, ()))
                else:
                    consumed = consumed.at_sequence(self.receipt_position(message.message))
                    message = next(source_rows, None)
            if record is None:
                consumed = consumed.at_offset(self.exhausted_offset(frontier, reader))
            if message is None:
                consumed = consumed.at_sequence(self.exhausted_sequence(frontier))
            return consumed, records
        finally:
            projected.close()
            native.close()

    def request_earlier(self, records: TranscriptSource, offset: int) -> bool:
        return records.request_earlier(offset)

    round_grace_limit = 16


class EarlierTranscript(TranscriptTraversal):
    def native(self, reader: TranscriptSource, cursor: TranscriptCursor, through: TranscriptCursor) -> Generator[TranscriptRecord, None, None]:
        return reader.reverse(cursor.offset)

    def source_rows(
        self,
        source: AssignedTranscriptSource,
        sequence: int,
        through: TranscriptCursor,
        limit: int,
    ) -> tuple[Delivered, ...]:
        return source.rows(before=sequence + 1, limit=limit)

    def chooses_native(self, native_time: float | None, receipt_time: float) -> bool:
        return native_time is None or native_time >= receipt_time

    def native_position(self, record: TranscriptRecord) -> int:
        return record.start

    def receipt_position(self, message: Delivered) -> int:
        return message.seq - 1

    def finish(self, records: list[tuple[TranscriptEvent, ...]]) -> tuple[TranscriptEvent, ...]:
        return tuple(event for record in reversed(records) for event in record)

    def bounds(
        self, initial: TranscriptCursor, consumed: TranscriptCursor,
    ) -> tuple[TranscriptCursor, TranscriptCursor]:
        return consumed, initial


class LaterTranscript(TranscriptTraversal):
    def request_earlier(self, records: TranscriptSource, offset: int) -> bool:
        return False

    def oriented(self, started: set[str], ended: set[str]) -> tuple[set[str], set[str]]:
        return started, ended

    def exhausted_offset(self, frontier: TranscriptCursor, reader: TranscriptSource) -> int:
        return frontier.offset

    def exhausted_sequence(self, frontier: TranscriptCursor) -> int:
        return frontier.wire.sequence

    def native(self, reader: TranscriptSource, cursor: TranscriptCursor, through: TranscriptCursor) -> Generator[TranscriptRecord, None, None]:
        return reader.forward(cursor.offset, through.offset)

    def source_rows(
        self,
        source: AssignedTranscriptSource,
        sequence: int,
        through: TranscriptCursor,
        limit: int,
    ) -> tuple[Delivered, ...]:
        return source.rows(after=sequence, before=through.wire.sequence + 1,
                           ascending=True, limit=limit)

    def chooses_native(self, native_time: float | None, receipt_time: float) -> bool:
        return native_time is None or native_time <= receipt_time

    def native_position(self, record: TranscriptRecord) -> int:
        return record.end

    def receipt_position(self, message: Delivered) -> int:
        return message.seq

    def finish(self, records: list[tuple[TranscriptEvent, ...]]) -> tuple[TranscriptEvent, ...]:
        return tuple(event for record in records for event in record)

    def bounds(
        self, initial: TranscriptCursor, consumed: TranscriptCursor,
    ) -> tuple[TranscriptCursor, TranscriptCursor]:
        return initial, consumed


class TranscriptSource(ReadsPublishedMessages, ResolvesSentReports):
    """Recorded items alone carry no assigned-wire or publication authority.

    Native and external stores supply their original records. An assigned
    source adds the separately acquired wire/publication capability below.
    """

    start = 0

    def committed_output_cut(self, location: NativeRecordLocation) -> SessionCut:
        """Only a native record source can corroborate SDK commit coordinates."""
        raise TranscriptChanged()

    def last_tool_call(self) -> EventTime:
        """A source without original producer tool times cannot prove activity."""
        return UnrecordedEventTime()

    def has_records(self, through: int) -> bool:
        return through > 0

    def valid_offset(self, offset: int, through: int) -> bool:
        return 0 <= offset <= through

    def has_older(self, offset: int) -> bool:
        return offset > self.start

    def request_earlier(self, offset: int) -> bool:
        return False

    async def watch_requests(self, emit: Callable[[ExternalPageRow], None]) -> None:
        """Sources without a harness continuation have no bridge requests."""

    def recorded_contexts(
        self, contexts: ReadsContextEntries, entry_ids: tuple[str, ...],
    ) -> dict[NativeItemId, SegmentManifest]:
        """Only a native journal records a context manifest per input entry."""
        return {}

    def voice(self, deal: Speaking) -> Voice:
        """A source that cannot read back the session's own words cannot hear an answer, so
        its deal never asks: asking would lock the session behind a question it can't answer."""
        return Mute()

    def turns(self, bound: TurnBound) -> tuple[TurnDigest, ...]:
        """Read input-bounded turns under one original record-source cut."""
        with self.reading() as reader:
            cut = reader.cut()
            if cut.offset == 0:
                return bound.take(())
            with closing(reader.reverse(cut.offset)) as records:
                turns = bound.take(self._newest_turns(records))
            if not cut.holds(reader.cut(), cut.offset):
                raise StaleRevision('Turn source changed during its read')
        return turns

    @staticmethod
    def _newest_turns(records: Iterator[NativeRecord]) -> Iterator[TurnDigest]:
        pending: list[NativeRecord] = []
        for record in records:
            pending.append(record)
            for digest in record.project(lambda original: (
                TurnDigest(
                    FieldCodec.decode(NativeTurnId, original.entry.require_entry_id()), tuple(
                    event for source in reversed(pending) for event in source.events)),
            ) if original.entry.input_boundary else ()):
                yield digest
                pending.clear()

    def current_draft(self, root: Path, incarnation: ThreadIncarnation) -> TranscriptDraft:
        with self.draft_snapshot(root, incarnation) as connection:
            return DraftRow.one(connection, thread=incarnation.require_recorded().lookup) or NoDraft()

    def draft_snapshot(self, root: Path, incarnation: ThreadIncarnation):
        return ExternalTranscript(root, incarnation).snapshot()

    def publication_revision(self, root: Path, recipient: RecordedThreadIncarnation):
        return PublishedReplyRevision(0, 0)

    @contextmanager
    def projection(self, source: TranscriptSource, routes: TranscriptRoutes):
        def project(records, *, max_records: int, max_bytes: int):
            return ((record, tuple(retained for event in record.events
                                   if (retained := event.for_external_source(source)) is not None))
                    for record in records)
        yield project

    def sent_messages(self, references: Iterable[MessageReference]) -> dict[MessageReference, Message]:
        return {}

    def resolve_sent(self, messages: tuple[SentTranscript, ...]):
        """Records without a sender assignment cannot qualify Send reports."""
        return tuple(messages)

    frontier = NoWireFrontier()

    def window(self, *, limit: int = 1, sent_limit: int = 0) -> AssignedSourceWindow:
        return AssignedSourceWindow(self.frontier, (), ())

    def require_cursor(self, cursor: TranscriptCursor) -> None:
        cursor.wire.require_native()

    def page_rows(
        self,
        traversal: TranscriptTraversal,
        sequence: int,
        through: TranscriptCursor,
        *,
        limit: int,
    ) -> Iterator[Delivered]:
        return iter(())

    def reply_revision(self, records: TranscriptSource) -> PublishedReplyRevision:
        return PublishedReplyRevision(0, 0)

    def native_records(
        self,
        fragments: Iterator[tuple[NativeRecord, ...]],
        routes: _SessionRoutes,
        reader: ReadsNativeInputs,
    ) -> Iterator[tuple[NativeRecord, tuple[TranscriptEvent, ...]]]:
        for fragment in fragments:
            for record in fragment:
                yield record, record.project(lambda original: self.native_events(
                    original, routes, original.entry.events, (),
                ))

    def native_events(
        self, record: NativeRecord, routes: _SessionRoutes,
        project_events: Callable[[TranscriptProjection], Iterable[TranscriptEvent]],
        references: tuple[MessageReference, ...],
    ) -> tuple[TranscriptEvent, ...]:
        entry = record.entry
        return tuple(project_events(TranscriptProjection(
            entry.routing_in(routes), routes.input_display(entry.input_id), position=record.start,
        )))


class NativeTranscript(ReadsNativeInputs, TranscriptSource):
    def __init__(self, path: Path):
        self.path = path

    def completed_input(self, context: NativeContextProof) -> bool:
        """Start at the proved input byte offset; never borrow the following input's final."""
        cut = self.cut()
        with closing(self.forward(context.session_entry_start, cut.offset)) as records:
            anchor = next(records)
            if not anchor.project(lambda original: original.entry.require_entry_id() == context.session_entry_id):
                raise TranscriptChanged("Native answer input changed after its proof was read")
            own_turn = takewhile(
                lambda record: not record.project(lambda original: original.entry.input_boundary), records)
            finals = tuple(record for record in own_turn if record.project(lambda original:
                original.entry.final_reply and any(
                    text for event in original.events for text in event.assistant_parts())))
            answered = anchor in self.inputs_for(finals, floor=context.session_entry_start)
        if not cut.holds(self.cut(), cut.offset):
            raise TranscriptChanged("Native answer source changed during its read")
        return answered

    def _tool_call_times(self, since: float = -inf) -> Generator[float]:
        try:
            with closing(self.tail()) as entries:
                for entry in entries:
                    timestamp = entry.timestamp.at.timestamp()
                    if timestamp < since:
                        return
                    if any(message.assistant and message.tool_calls
                           for message in entry.messages):
                        yield timestamp
        except FileNotFoundError:
            return

    def last_tool_call(self) -> EventTime:
        with closing(self._tool_call_times()) as times:
            return next(
                (RecordedEventTime(timestamp) for timestamp in times), UnrecordedEventTime(),
            )

    @contextmanager
    def reading(self):
        """Keep the native record owner bound across one page projection."""
        yield self

    def imported_sources(self):
        """Read original metadata before conversation, including native configuration.

        The importer writes its metadata after the header. A native saved-file
        producer may also prepend model/settings records. Stop at conversation;
        never scan the remaining history or claim this was a model request.
        """
        revision = FileRevision.from_stat(self.path.stat())
        with self.path.open("rb") as stream:
            SessionEntry.read(stream.readline())
            sources = []
            for raw in stream:
                entry = NativeEntry.read(raw)
                if isinstance(entry, MessageEntry):
                    break
                sources.extend(entry.imported_sources())
        if FileRevision.from_stat(self.path.stat()) != revision:
            raise ValueError("Imported context saved source changed while reading")
        return tuple(sources)

    def cut(self) -> SessionCut:
        try:
            stream = self.path.open("rb")
        except FileNotFoundError:
            return MissingSessionCut()
        # One open file supplies both the revision and its last record boundary.
        with stream:
            revision = FileRevision.from_file(stream)
            return PresentSessionCut(revision, record_end(stream, revision.size))

    def committed_output_cut(self, location: NativeRecordLocation) -> SessionCut:
        """Corroborate one indexed output against its original header and row."""
        if self.path.absolute() != location.path:
            raise TranscriptChanged()
        with self.path.open("rb") as stream:
            revision = FileRevision.from_file(stream)
            header = SessionEntry.read(stream.readline())
            if header.id != location.session_id or revision.size < location.session_entry_end:
                raise TranscriptChanged()
            stream.seek(location.session_entry_start - 1)
            if stream.read(1) != b"\n":
                raise TranscriptChanged()
            raw = stream.read(location.session_entry_end - location.session_entry_start)
            if (len(raw) != location.session_entry_end - location.session_entry_start
                    or not raw.endswith(b"\n")):
                raise TranscriptChanged()
            location.require_entry(NativeEntry.read(raw))
            return PresentSessionCut(revision, location.session_entry_end)

    def publication_revision(self, root: Path, recipient: RecordedThreadIncarnation):
        return self.cut().publication_revision(root, self, recipient)

    @contextmanager
    def projection(self, sources, routes):
        """Project records through their native input and the session's routes."""
        with routes.for_session(str(self.path)) as session_routes:
            def project(records, *, max_records, max_bytes):
                return sources.native_records(
                    self.fragments(records, max_records=max_records, max_bytes=max_bytes),
                    routes=session_routes, reader=self)

            yield project

    def fragments(self, records, *, max_records, max_bytes):
        """Borrow original decoded records within the caller's page budget.

        An oversized original stays whole. No cursor advances during capture;
        the traversal still owns consumption of each independent source.
        """
        fragment, used = [], 0
        for record in records:
            if fragment and used + record.size > max_bytes:
                yield tuple(fragment)
                fragment, used = [], 0
            fragment.append(record)
            used += record.size
            if len(fragment) >= max_records or used >= max_bytes:
                yield tuple(fragment)
                fragment, used = [], 0
        if fragment:
            yield tuple(fragment)

    def recorded_contexts(self, contexts, entry_ids):
        return contexts.entries(NativeSessionIdentity(self.session_id, str(self.path)),
                               tuple(NativeItemId(identity) for identity in entry_ids))

    @property
    def session_id(self):
        with self.path.open("rb") as stream:
            try:
                entry = SessionEntry.read(stream.readline())
            except MalformedRecord:
                return None
        return entry.id

    def annotation_ids(self, after: int, through: int) -> tuple[FreshNativeId, ...]:
        return tuple(identity for record in self.forward(after, through)
                     for identity in record.annotation_id.present())

    def require_publication_context(
        self,
        user: MessageEntry,
        *,
        session_entry_start: int,
        files: NativeSessionFiles,
    ) -> NativeContextProof:
        """Corroborate the existing native producer's context, without replay.

        Native inputDigest identifies a request envelope, not plain text. The
        original STARTED ledger separately proves the exact sent text. Native's
        journal owns context inclusion for the actual header/input/entry identity;
        this bounded indexed lookup does not guess a request kind or options.
        """
        files.require_private_directory(self.path.parent)
        with self.path.open("rb") as stream:
            header = SessionEntry.read(stream.readline())
        with NativeContextJournal.open_evidence(files.input_proof) as db:
            try:
                proof = NativeContextJournal.for_input(db, user.require_input_id())
            except NativePiUnavailable as missing:
                raise MalformedRecord(
                    "Native publication has no committed context inclusion"
                ) from missing
            return proof.corroborate(
                self.path.absolute(),
                NativeInputLocation(
                    user.require_input_id(),
                    header.id,
                    user.id.require_present(),
                    session_entry_start=session_entry_start,
                ),
            )

    def inputs_for(self, records: Sequence[NativeRecord], *, floor: int = 0) -> tuple[NativeRecord, ...]:
        """Find the user input behind each record in one reverse walk for the batch.

        Parent IDs are followed back from the latest record; every intervening
        record is visited once for the whole batch, and the walk stops when each
        chain reaches a user boundary or a root. Tool rounds and branches cannot
        lend another input's publication proof.
        """
        found: list[NativeRecord] = [NoNativeInput()] * len(records)
        waiting: dict[str | RootEntry, list[int]] = defaultdict(list)
        latest = 0
        for position, record in enumerate(records):
            entry = record.entry
            if entry is None:
                continue
            if entry.input_boundary:
                found[position] = record
            else:
                waiting[entry.parent_id].append(position)
                latest = max(latest, record.start)
        # A chain that reaches a root (no parent) has no input.
        waiting.pop(RootEntry(), [])
        if waiting:
            for ancestor in self.reverse(latest, floor=floor):
                entry = ancestor.entry
                if entry is None or entry.id not in waiting:
                    continue
                chain = waiting.pop(entry.id)
                if entry.input_boundary:
                    for position in chain:
                        found[position] = ancestor
                else:
                    waiting[entry.parent_id].extend(chain)
                    waiting.pop(RootEntry(), [])
                if not waiting:
                    break
        return tuple(found)

    def input_for(self, record: NativeRecord) -> NativeRecord:
        """Follow the native record's parents to find the user input that started it."""
        return self.inputs_for((record,))[0]

    def publication_input(self, *, after: int, through: int, files: NativeSessionFiles):
        """Return the final producer and its actual tracked input ancestry.

        This bounded reverse walk runs before any BUS/registry lock is acquired.
        A display-tolerant malformed record cannot supply publication evidence.
        """
        for record in self.reverse(through):
            if record.start < after:
                break
            if not record.is_final_publication():
                continue
            user = self.input_for(record)
            entry = user.require_entry().require_tracked_user()
            self.require_publication_context(entry, session_entry_start=user.start, files=files)
            return record, entry
        raise RelationViolationError("Native turn has no final reply after its checkpoint")

    """How far back receipts read: a Pi record carrying tool output is large, so a smaller
    window can hold no turn's end."""

    def voice(self, deal: Speaking) -> Voice:
        """A native thread can receive the question; its own audit carries the answer."""
        return deal

    def tail(self, *, max_bytes: int | None = None):
        end = self.path.stat().st_size
        floor = max(0, end - max_bytes) if max_bytes is not None else 0
        for _, _, raw in reverse_records(self.path, end, floor=floor):
            # Skip only the record still being appended; corrupt history raises.
            if raw.endswith(b"\n"):
                yield NativeEntry.read(raw)

    def forward(self, after: int, through: int):
        with self.path.open("rb") as stream:
            stream.seek(after)
            while stream.tell() < through:
                start = stream.tell()
                raw = stream.readline(through - start)
                if not raw:
                    return
                yield NativeRecord.read(start, stream.tell(), raw)

    def reverse(self, before: int, *, floor: int = 0) -> Generator[NativeRecord]:
        for start, end, raw in reverse_records(self.path, before, floor=floor):
            yield NativeRecord.read(start, end, raw)


@dataclass(frozen=True, slots=True)
class TranscriptCursor(DeclaredFamily, affix="TranscriptCursor"):
    """A native transcript position; WireTranscriptCursor adds an assigned wire source."""

    session_file: ClassVar[str | None] = None
    offset: ClassVar[int] = 0
    wire: ClassVar[WireFrontier] = NoWireFrontier()

    def indexed_by(self, current: RecordsCut) -> bool:
        """Whether the current source still indexes this position. A position in no file's bytes
        has nothing a rewrite can move."""
        return True

    def at_offset(self, offset: int) -> TranscriptCursor:
        return replace(self, offset=offset)

    def at_sequence(self, sequence: int) -> TranscriptCursor:
        return self.with_frontier(self.wire.at(sequence))

    @abstractmethod
    def with_frontier(self, frontier: WireFrontier) -> TranscriptCursor: ...

    @abstractmethod
    def mark_read(self, reads: MarksSourceRead[SessionCut], viewer: str, runtime: TranscriptRuntime) -> None: ...

    def contains(self, other: TranscriptCursor) -> bool:
        return other.within(self)

    def same_position(self, other: TranscriptCursor) -> bool:
        return self == other

    def accepts_source(self, other: TranscriptCursor) -> bool:
        return self.session_file == other.session_file

    def within(self, other: TranscriptCursor) -> bool:
        return (self.accepts_source(other) and self.offset <= other.offset
                and self.wire.within(other.wire))

    def earlier_page(self) -> PagePosition:
        """The page that ends where this read position starts."""
        return EarlierThan(self)

    def later_page(self) -> PagePosition:
        """The page that starts where this read position ends."""
        return LaterThan(self)

    def page_limit(self) -> PageLimit:
        """A painted read position pins the page to the source it was read from."""
        return Through(self)

    def matches_runtime(self, runtime: TranscriptRuntime) -> bool:
        return self.session_file == runtime.read_key

    def require_readable(
        self, runtime: TranscriptRuntime, reader: TranscriptSource, current: RecordsCut, through: int,
    ) -> None:
        if (not self.matches_runtime(runtime) or not reader.valid_offset(self.offset, through)
                or not self.indexed_by(current)):
            raise TranscriptChanged()

    def requires_commit(self) -> bool:
        """A changed transcript at a read position needs no commit before it is read again."""
        return False

    def painted_read[Result](self, acknowledge: Callable[[TranscriptCursor], Result],
                             nothing: Result) -> Result:
        """Choose painted acknowledgement work before any reader or write lock is opened."""
        return acknowledge(self)

    async def read_page[Page](self, loader: Callable[..., Awaitable[Page]],
                        position: PagePosition) -> Page:
        return await loader(position=position, limit=self.page_limit())

    def covers_incoming(self, sequence: int) -> bool:
        # Callers hold a canonical recipient assignment, never an arbitrary seq.
        return 0 < sequence <= self.wire.sequence

    def over_native(self, prefixes: Sequence[TranscriptCursor]) -> TranscriptCursor:
        """This wire position over the furthest committed native prefix within it: wire
        assignment can advance without transferring anonymous native output."""
        within = tuple(prefix for prefix in prefixes if prefix.wire.within(self.wire))
        prefix = max((prefix for prefix in within if prefix.session_file == self.session_file),
                     key=lambda prefix: prefix.offset, default=self.inherited_prefix(within))
        return self.with_prefix(prefix)

    def inherited_prefix(self, prefixes: Sequence[TranscriptCursor]) -> TranscriptCursor:
        return self.at_offset(0)

    def with_prefix(self, prefix: TranscriptCursor) -> TranscriptCursor:
        return self.at_offset(prefix.offset)


@dataclass(frozen=True, slots=True)
class UnreadTranscriptCursor(TranscriptCursor):
    """No source has been read; this is not a source position or a read mark."""

    def contains(self, other: TranscriptCursor) -> bool:
        return False

    def within(self, other: TranscriptCursor) -> bool:
        return False

    def same_position(self, other: TranscriptCursor) -> bool:
        return False

    def accepts_source(self, other: TranscriptCursor) -> bool:
        return True

    def earlier_page(self) -> PagePosition:
        """Nothing read bounds no page: the latest one."""
        return LatestPage()

    def later_page(self) -> PagePosition:
        return LatestPage()

    def page_limit(self) -> PageLimit:
        """Nothing read pins no source window; the page's own frontier applies."""
        return AtFrontier()

    def requires_commit(self) -> bool:
        """A replaced source with nothing read must be committed before it is read again."""
        return True

    def painted_read[Result](self, acknowledge: Callable[[TranscriptCursor], Result],
                             nothing: Result) -> Result:
        return nothing

    def at_offset(self, offset: int) -> TranscriptCursor:
        return self

    def with_frontier(self, frontier: WireFrontier) -> TranscriptCursor:
        return self

    def mark_read(self, reads: MarksSourceRead[SessionCut], viewer: str, runtime: TranscriptRuntime) -> None:
        """Nothing has been drawn, so there is nothing to acknowledge."""


class FileTranscriptCursor(TranscriptCursor, declared_name=None):
    """A position in a file's bytes: a session rewritten through os.replace is another file,
    and every old offset past 0 indexes nothing in it, even where it still fits."""

    __slots__ = ()
    file: FileIdentity | MissingFile

    def indexed_by(self, current: RecordsCut) -> bool:
        return current.indexes(self.file, self.offset)


@dataclass(frozen=True, slots=True)
class NativeTranscriptCursor(FileTranscriptCursor):
    session_file: str = field()
    offset: int = field()
    file: FileIdentity | MissingFile = field(kw_only=True)

    def inherited_prefix(self, prefixes: Sequence[TranscriptCursor]) -> TranscriptCursor:
        return next(iter(prefixes), self.at_offset(0))

    def with_prefix(self, prefix: TranscriptCursor) -> TranscriptCursor:
        return prefix.with_frontier(self.wire)

    def with_frontier(self, frontier: WireFrontier) -> TranscriptCursor:
        return frontier.cursor(self.session_file, self.offset, file=self.file)

    def mark_read(self, reads: MarksSourceRead[SessionCut], viewer: str, runtime: TranscriptRuntime) -> None:
        if self.session_file != runtime.read_key:
            raise TranscriptChanged()
        for key in runtime.read_keys():
            # A mark painted from a replaced file would count the new file read through an
            # offset that indexes nothing in it.
            current = NativeTranscript(Path(key)).cut()
            if not self.indexed_by(current):
                raise TranscriptChanged()
            reads.mark_read(viewer, key, self.offset, current)


@dataclass(frozen=True, slots=True)
class WireTranscriptCursor(NativeTranscriptCursor):
    wire: AssignedSourceCursor = field()


@dataclass(frozen=True)
class PagePosition(DeclaredFamily, affix=""):
    """Where a bounded transcript page starts: the latest page, or one side of a read cursor."""

    def require_readable(
        self, runtime: TranscriptRuntime, reader: TranscriptSource, current: RecordsCut, through: int,
    ) -> None:
        """The latest page has no cursor to validate."""

    def within(self, frontier: TranscriptCursor) -> bool:
        return True

    def initial(self, frontier: TranscriptCursor) -> TranscriptCursor:
        return frontier

    def traversal(self) -> TranscriptTraversal:
        return EarlierTranscript()

    def shows_fork_start(self, frontier: TranscriptCursor) -> bool:
        """An inherited thread's fork origin closes its oldest page; the latest page reaches it."""
        return True

    def later_only(self) -> PagePosition:
        """A re-read under a new limit keeps only a later-than cursor; any other page starts latest."""
        return LatestPage()


@dataclass(frozen=True, slots=True)
class LatestPage(PagePosition):
    """The newest page up to the frontier."""


@dataclass(frozen=True, slots=True)
class CursorPage(PagePosition, declared_name=None):
    """A page beside a read cursor, which must belong to the page's source and fit its cut."""

    cursor: TranscriptCursor

    def require_readable(
        self, runtime: TranscriptRuntime, reader: TranscriptSource, current: RecordsCut, through: int,
    ) -> None:
        self.cursor.require_readable(runtime, reader, current, through)

    def within(self, frontier: TranscriptCursor) -> bool:
        return frontier.contains(self.cursor)

    def initial(self, frontier: TranscriptCursor) -> TranscriptCursor:
        return self.cursor


@dataclass(frozen=True, slots=True)
class EarlierThan(CursorPage):
    def shows_fork_start(self, frontier: TranscriptCursor) -> bool:
        return self.cursor == frontier


@dataclass(frozen=True, slots=True)
class LaterThan(CursorPage):
    def traversal(self) -> TranscriptTraversal:
        return LaterTranscript()

    def shows_fork_start(self, frontier: TranscriptCursor) -> bool:
        return False

    def later_only(self) -> PagePosition:
        return self


@dataclass(frozen=True)
class PageLimit(DeclaredFamily, affix=""):
    """How far a page's source reaches: its live frontier, or a cursor already painted."""

    def bound(self, frontier: TranscriptCursor) -> TranscriptCursor:
        return frontier

    def accepts_runtime(self, runtime: TranscriptRuntime) -> bool:
        """An unpinned page uses the thread's first available transcript source."""
        return True


@dataclass(frozen=True, slots=True)
class AtFrontier(PageLimit):
    """The page reaches the source's current frontier."""


@dataclass(frozen=True, slots=True)
class Through(PageLimit):
    """The page reaches no further than a cursor already painted, in that cursor's source."""

    cursor: TranscriptCursor

    def bound(self, frontier: TranscriptCursor) -> TranscriptCursor:
        return self.cursor

    def accepts_runtime(self, runtime: TranscriptRuntime) -> bool:
        return self.cursor.matches_runtime(runtime)


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


@dataclass(frozen=True)
class AssignedTranscriptSource(TranscriptSource):
    root: Path
    recipient: RecordedThreadIncarnation
    bus: Bus

    @classmethod
    def for_thread(cls, root: Path, thread: ReadsThreadIncarnation[ThreadIncarnation], bus: Bus) -> Self:
        return cls(root, thread.incarnation.require_recorded(), bus)

    @classmethod
    def open(cls, identity: AssignedSourceIdentity):
        """Open only the recorded wire, without current registry or SQL authority."""
        root = Path(identity.root)
        return identity.recipient.recorded_or(
            lambda recorded: cls(root, recorded, Bus(Bus.path_in(root))),
            TranscriptSource,
        )

    def assignments(self, references: Iterable[MessageReference]) -> dict[MessageReference, Delivered]:
        references = tuple(references)
        if not references:
            return {}
        return {original.message.reference: original for original in self.rows(
            sequences=tuple(reference.seq for reference in references), limit=len(references),
        )}

    def sent_messages(self, references: Iterable[MessageReference]) -> dict[MessageReference, Message]:
        return {reference: original.message
                for reference, original in self.assignments(references).items()
                if original.sender_lookup == self.recipient.lookup}

    def resolve_sent(
        self, messages: tuple[SentTranscript, ...],
    ) -> tuple[SentTranscript | PublishedSentMessage, ...]:
        """Qualify reports against exact full rows in one bounded wire read.

        A report is not publication evidence. Keep it unchanged unless this
        sender's frozen assignments supply its reference, body, time and route.
        Missing births cannot select an assignment, even if a name matches.
        """
        messages = tuple(messages)
        originals = self.sent_messages(message.source for message in messages)
        scope = AssignedSourceIdentity(str(self.root), self.recipient)
        return tuple(
            PublishedSentMessage.from_originals(message, scope, originals)
            for message in messages
        )

    def require_cursor(self, cursor: TranscriptCursor) -> None:
        cursor.wire.require_source(AssignedSourceIdentity(str(self.root), self.recipient))

    @classmethod
    def for_recorded(cls, root: Path, thread: ReadsThreadIncarnation[ThreadIncarnation], bus: Bus, records: TranscriptSource):
        """Only the original recorded birth can select frozen assignments."""
        return thread.incarnation.recorded_or(
            lambda recorded: cls(root, recorded, bus), lambda: records
        )

    def rows(
        self, *, before: int | None = None, after: int = 0, limit: int = 1,
        ascending: bool = False, sequences: tuple[int, ...] = (),
    ) -> tuple[Delivered, ...]:
        return tuple(
            delivery for record in self.bus.conversation(
                self.recipient.lookup, before=before, after=after, limit=limit,
                descending=not ascending, sequences=sequences,
            ) for delivery in record.deliveries()
        )

    @property
    def frontier(self):
        rows = self.rows(limit=1)
        return AssignedSourceCursor(
            str(self.root), self.recipient, rows[0].seq if rows else 0,
        )

    def window(self, *, limit: int = 1, sent_limit: int = 0) -> AssignedSourceWindow:
        """The same sequence cut bounds both conversation and sender pages."""
        before = self.bus.revision() + 1
        rows = self.rows(before=before, limit=limit)
        sent = tuple(
            delivery for record in self.bus.sent(
                self.recipient.lookup, before=before, limit=sent_limit,
            ) for delivery in record.deliveries()
        )
        newest = max((row.seq for row in (*rows, *sent)), default=0)
        cursor = AssignedSourceCursor(str(self.root), self.recipient, newest)
        return AssignedSourceWindow(cursor, rows, sent)

    def page_rows(
        self,
        traversal: TranscriptTraversal,
        sequence: int,
        through: TranscriptCursor,
        *,
        limit: int,
    ) -> Iterator[Delivered]:
        return iter(traversal.source_rows(self, sequence, through, limit))

    def native_events(
        self, record: NativeRecord, routes: _SessionRoutes,
        project_events: Callable[[TranscriptProjection], Iterable[TranscriptEvent]],
        references: tuple[MessageReference, ...],
    ) -> tuple[TranscriptEvent, ...]:
        entry = record.entry
        routing = entry.routing_in(routes)
        events = super().native_events(record, routes, project_events, references)
        published = entry.final_reply and any(
            refs and self.sent_messages(refs).keys() >= set(refs)
            for refs in (routing.publications, references)
        )

        return tuple(retained for event in events
                     if (retained := event.for_assigned_source(self, published)) is not None)

    def events(self, original: Delivered) -> tuple[TextTranscript, ...]:
        if original.sender_lookup == self.recipient.lookup:
            return (
                SentTranscript.from_message(original),
            )
        return (
            IncomingTranscript.of(original, reader=self.recipient.name),
        )


class LiveAssignedTranscriptSource(AssignedTranscriptSource):
    """Live publication evidence is additional to original wire assignments."""

    def reply_revision(self, records: TranscriptSource) -> PublishedReplyRevision:
        return records.publication_revision(self.root, self.recipient)

    def native_records(
        self,
        fragments: Iterator[tuple[NativeRecord, ...]],
        routes: _SessionRoutes,
        reader: ReadsNativeInputs,
    ) -> Iterator[tuple[NativeRecord, tuple[TranscriptEvent, ...]]]:
        """Native ancestry precedes one atomic SQL read per bounded fragment."""
        from agent_comms.transcript.transcripts import TranscriptRead

        lookup = self.recipient.lookup
        for fragment in fragments:
            fragment = tuple(fragment)
            selected = tuple(zip(fragment, reader.inputs_for(fragment), strict=True))
            with TranscriptRead.publication_source(self.root) as db:
                # Missing coordination remains detached native history. Do not
                # introduce header failures where no SQL source was selected.
                session_id = (reader.session_id
                              if db is not None and any(
                                  user.input_reported for _, user in selected) else None)
                capture = partial(
                    NativeRuntimeInput.transcript_projection,
                    db, reader, owner_lookup=lookup, session_id=session_id,
                )
                originals = tuple((record, record.project(partial(capture, user=user.entry)))
                                  for record, user in selected)
            for record, projection in originals:
                yield record, record.project(
                    lambda record: self.native_events(record, routes, *projection)
                )
