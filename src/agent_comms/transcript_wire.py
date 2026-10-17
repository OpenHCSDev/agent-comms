"""The canonical wire owns incoming and outgoing conversation history.

Native bytes and committed wire sequences are independent source coordinates.
A display cursor advances each only when that source's record is consumed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from . import sent_message
from .declared_family import DeclaredFamily
from .native_entries import TranscriptProjection
from .native_runtime_input import NativeRuntimeInput, PublishedReplyRevision

from .bus_publication import CommittedDelivery
from .routing import MessageRoute, TurnRouting
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .transcript_events import AssistantTranscript, IncomingTranscript, SentTranscript, TranscriptEvent, UserTranscript, ToolEndTranscript, ToolStartTranscript
from .wire_log import ConversationSourceWindowRequest, WireLog


@dataclass(frozen=True)
class TranscriptSource(DeclaredFamily, affix="TranscriptSource"):
    def position(self, session_file: str, offset: int) -> TranscriptCursor:
        return NativeTranscriptCursor(session_file, offset)


class NativeTranscriptSource(TranscriptSource, declared_name="native"):
    pass


@dataclass(frozen=True)
class AssignedSourceIdentity:
    root: str
    recipient: ThreadIncarnation

    @property
    def publication_key(self) -> tuple[str, float]:
        """Published authorship survives a rename, not a new birth or wire."""
        return self.root, self.recipient.require_recorded().created_at

    def same_publication_source(self, other: AssignedSourceIdentity) -> bool:
        return self.root == other.root and self.recipient.same_publication_birth(other.recipient)


@dataclass(frozen=True)
class AssignedSourceCursor(TranscriptSource, declared_name="wire"):
    root: str
    recipient: RecordedThreadIncarnation
    sequence: int

    def __post_init__(self):
        if self.sequence < 0:
            raise ValueError("Wire sequence cannot be negative")

    def position(self, session_file: str, offset: int) -> TranscriptCursor:
        return WireTranscriptCursor(session_file, offset, self)

    @property
    def identity(self) -> AssignedSourceIdentity:
        return AssignedSourceIdentity(self.root, self.recipient)

    def require_source(self, identity: AssignedSourceIdentity) -> None:
        if self.identity != identity:
            raise ValueError("Wire cursor belongs to a different transcript source")

    def at(self, sequence: int) -> AssignedSourceCursor:
        return replace(self, sequence=sequence)


@dataclass(frozen=True)
class AssignedSourceWindow:
    """Conversation and sent sources captured with one wire frontier."""

    source: TranscriptSource
    sources: tuple[CommittedDelivery, ...]
    sent_sources: tuple[CommittedDelivery, ...]


@dataclass(frozen=True, slots=True)
class TranscriptCursor(DeclaredFamily, affix="TranscriptCursor"):
    """A native transcript position; its wire member adds an assigned source."""

    session_file: str
    offset: int

    @staticmethod
    def at(session_file: str, offset: int, source: TranscriptSource) -> TranscriptCursor:
        return source.position(session_file, offset)

    def at_offset(self, offset: int) -> TranscriptCursor:
        return replace(self, offset=offset)

    def at_sequence(self, sequence: int) -> TranscriptCursor:
        if sequence:
            raise ValueError("Native-only cursor cannot advance an assigned wire source")
        return self

    def contains(self, other: TranscriptCursor) -> bool:
        return other.native_within(self) and other.wire_within(self)

    def native_within(self, parent: TranscriptCursor) -> bool:
        return self.session_file == parent.session_file and self.offset <= parent.offset

    def wire_within(self, parent: TranscriptCursor) -> bool:
        return True

    def holds_wire(self, cursor: WireTranscriptCursor) -> bool:
        return False

    def wire_rows(self, records, traversal, cursor, *, limit):
        return ()

    def wire_predicate(self, traversal, through):
        return traversal.native_prefix(through)

    def require_source(self, identity: AssignedSourceIdentity) -> None:
        """A native-only cursor fits every source."""

    def require_native(self) -> None:
        """A native-only cursor is what recorded items carry."""

    def resolve_sent(self, reports):
        return tuple(reports)

    def covers_incoming(self, sequence: int) -> bool:
        return False

    def over_native(self, prefixes: Sequence[TranscriptCursor]) -> TranscriptCursor:
        """Retain this wire position over the furthest committed native prefix."""
        within = tuple(prefix for prefix in prefixes if prefix.wire_within(self))
        prefix = max((prefix for prefix in within if prefix.session_file == self.session_file),
                     key=lambda prefix: prefix.offset, default=next(iter(within), self.at_offset(0)))
        return replace(self, session_file=prefix.session_file, offset=prefix.offset)


@dataclass(frozen=True, slots=True)
class NativeTranscriptCursor(TranscriptCursor):
    pass


@dataclass(frozen=True, slots=True)
class WireTranscriptCursor(TranscriptCursor):
    wire: AssignedSourceCursor

    def at_sequence(self, sequence: int) -> TranscriptCursor:
        return replace(self, wire=self.wire.at(sequence))

    def wire_within(self, parent: TranscriptCursor) -> bool:
        return parent.holds_wire(self)

    def holds_wire(self, cursor: WireTranscriptCursor) -> bool:
        return self.wire.identity == cursor.wire.identity and cursor.wire.sequence <= self.wire.sequence

    def covers_incoming(self, sequence: int) -> bool:
        return 0 < sequence <= self.wire.sequence

    def wire_rows(self, records, traversal, cursor, *, limit):
        predicate, parameters = cursor.wire_predicate(traversal, self.wire)
        return records.rows(predicate, parameters, ascending=traversal.ascending, limit=limit)

    def wire_predicate(self, traversal, through):
        return traversal.predicate(self.wire.sequence, through)

    def require_source(self, identity: AssignedSourceIdentity) -> None:
        self.wire.require_source(identity)

    def require_native(self) -> None:
        raise ValueError("Recorded items cannot acquire an assigned wire cursor")

    def resolve_sent(self, reports):
        root = Path(self.wire.root)
        return AssignedTranscriptSource(root, self.wire.recipient, WireLog(root / "bus.jsonl")).resolve_sent(reports)


class TranscriptTraversal(ABC):
    @abstractmethod
    def native(self, reader, cursor, through): ...

    @abstractmethod
    def predicate(self, sequence, through): ...

    @abstractmethod
    def native_prefix(self, through): ...

    @abstractmethod
    def chooses_native(self, native_time, wire_time): ...

    @abstractmethod
    def native_position(self, record): ...

    @abstractmethod
    def wire_position(self, message): ...

    @abstractmethod
    def finish(self, records): ...

    @abstractmethod
    def bounds(self, initial, consumed): ...

    def round_edges(self, batch: tuple["TranscriptEvent", ...]) -> tuple[set[str], set[str]]:
        """The tool-call ids a batch opens and closes, by traversal direction.

        A saved tool round is its call record together with its result record;
        a page window never stops between them.
        """
        opening = ToolEndTranscript if self.descending_rounds else ToolStartTranscript
        closing = ToolStartTranscript if self.descending_rounds else ToolEndTranscript
        return (
            {event.tool_call_id for event in batch if isinstance(event, opening)},
            {event.tool_call_id for event in batch if isinstance(event, closing)},
        )

    def read_records(self, reader, project, source, initial, frontier,
                     *, max_messages, max_bytes):
        """Consume each original source coordinate only when its record is read."""
        consumed, records, used = initial, [], 0
        # A tool round opened by an already appended record keeps the window open
        # until its paired record is appended too; the bounded grace keeps an
        # interrupted call (its result record never arrives) from growing a page.
        open_rounds: set[str] = set()
        round_grace = 0
        native = self.native(reader, initial, frontier) if frontier.offset else (record for record in ())
        projected = project(native, max_records=max_messages + 1, max_bytes=max_bytes)
        try:
            record, events = next(projected, (None, ()))
            source_rows = iter(frontier.wire_rows(source, self, consumed, limit=max_messages + 1))
            message = next(source_rows, None)
            while record is not None or message is not None:
                if record is not None and not events:
                    if self.ascending and record.incomplete_tail(frontier.offset):
                        break
                    consumed = consumed.at_offset(self.native_position(record))
                    record, events = next(projected, (None, ()))
                    continue
                # NativeEntry owns one clock for all parts of a record.
                native_time = events[0].timestamp if events else None
                choose_native = record is not None and (
                    message is None
                    or self.chooses_native(native_time, message.message.timestamp)
                )
                batch = events if choose_native else source.events(message)
                cost = record.size if choose_native else sum(event.text_size for event in batch)
                if records and (len(records) >= max_messages or used + cost > max_bytes):
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
                    consumed = frontier.at_sequence(self.wire_position(message.message)).at_offset(consumed.offset)
                    message = next(source_rows, None)
            if record is None:
                consumed = consumed.at_offset(frontier.offset if self.ascending else 0)
            if message is None:
                consumed = frontier.at_offset(consumed.offset) if self.ascending else consumed.at_sequence(0)
            return consumed, records
        finally:
            projected.close()
            native.close()

    ascending = False
    # Reading newest-first meets a round's result before its call.
    descending_rounds = True
    round_grace_limit = 16


class EarlierTranscript(TranscriptTraversal):
    def native(self, reader, cursor, through):
        return reader.reverse(cursor.offset)

    def predicate(self, sequence, through):
        return "w.seq<=?", (sequence,)

    def native_prefix(self, through):
        return "FALSE", ()

    def chooses_native(self, native_time, wire_time):
        return native_time is None or native_time >= wire_time

    def native_position(self, record):
        return record.start

    def wire_position(self, message):
        return message.seq - 1

    def finish(self, records):
        return tuple(event for record in reversed(records) for event in record)

    def bounds(self, initial, consumed):
        return consumed, initial


class LaterTranscript(TranscriptTraversal):
    ascending = True
    descending_rounds = False

    def native(self, reader, cursor, through):
        return reader.forward(cursor.offset, through.offset)

    def predicate(self, sequence, through):
        return "w.seq>? AND w.seq<=?", (sequence, through.sequence)

    def native_prefix(self, through):
        return "w.seq<=?", (through.sequence,)

    def chooses_native(self, native_time, wire_time):
        return native_time is None or native_time <= wire_time

    def native_position(self, record):
        return record.end

    def wire_position(self, message):
        return message.seq

    def finish(self, records):
        return tuple(event for record in records for event in record)

    def bounds(self, initial, consumed):
        return initial, consumed


class TranscriptRecords:
    """Project original records; capture their native or assigned wire source."""

    def capture_source(self) -> TranscriptSource:
        return NativeTranscriptSource()

    def window(self, *, limit=1, sent_limit=0) -> AssignedSourceWindow:
        return AssignedSourceWindow(self.capture_source(), (), ())

    def require_cursor(self, cursor):
        cursor.require_native()

    def reply_revision(self, records):
        return PublishedReplyRevision(0, 0)

    def native_records(self, fragments, routes, reader):
        for fragment in fragments:
            for record in fragment:
                yield record, record.project(lambda original: self.native_events(
                    original, routes, original.entry.events, (),
                ))

    def native_events(self, record, routes, project_events, references):
        entry = record.entry
        return tuple(project_events(TranscriptProjection(
            routes.get(entry.id), routes.input_display(entry.input_id),
        )))


@dataclass(frozen=True)
class AssignedTranscriptSource(TranscriptRecords):
    root: Path
    recipient: RecordedThreadIncarnation
    log: WireLog

    @classmethod
    def for_thread(cls, root, thread, log):
        return cls(root, thread.incarnation.require_recorded(), log)

    def resolve_sent(self, messages):
        """Qualify reports against exact full rows in one bounded wire read.

        A report is not publication evidence. Keep it unchanged unless this
        sender's frozen assignments supply its reference, body, time and route.
        Missing births cannot select an assignment, even if a name matches.
        """
        messages = tuple(messages)
        if not messages:
            return messages
        lookup = self.recipient.lookup
        marks = ",".join("?" for _ in messages)
        originals = {
            original.message.reference: original.message
            for original in self.rows(
                f"w.seq IN ({marks}) AND w.sender_lookup=?",
                (*[message.source.seq for message in messages], lookup),
                limit=len(messages),
            )
        }
        scope = AssignedSourceIdentity(str(self.root), self.recipient)
        return tuple(
            sent_message.PublishedSentMessage(message, scope)
            if (original := originals.get(message.source)) is not None
            and SentTranscript.from_message(original) == message else message
            for message in messages
        )

    def require_cursor(self, cursor):
        cursor.require_source(AssignedSourceIdentity(str(self.root), self.recipient))

    @classmethod
    def for_recorded(cls, root, thread, log, records):
        """Only the original recorded birth can select frozen assignments."""
        return thread.incarnation.recorded_or(
            lambda recorded: cls(root, recorded, log), lambda: records
        )

    def rows(
        self, predicate="1", parameters=(), *, limit=1, ascending=False,
    ) -> Iterator[CommittedDelivery]:
        return self.log.conversation_sources(
            self.recipient.lookup,
            predicate,
            parameters,
            limit=limit,
            ascending=ascending,
        )

    def capture_source(self) -> AssignedSourceCursor:
        lookup = self.recipient.lookup
        return AssignedSourceCursor(
            str(self.root), self.recipient,
            ConversationSourceWindowRequest.frontier(self.log, lookup),
        )

    def window(self, *, limit=1, sent_limit=0) -> AssignedSourceWindow:
        """One certified cut owns conversation, sent rows and their frontier."""
        lookup = self.recipient.lookup
        conversation_request = ConversationSourceWindowRequest("1", (), limit, False)
        sent_request = ConversationSourceWindowRequest(
            "w.sender_lookup=?", (lookup,), sent_limit, False
        )
        conversation, sent = conversation_request.capture_many(
            self.log, lookup, (conversation_request, sent_request)
        )
        rows = tuple(conversation)
        cursor = AssignedSourceCursor(
            str(self.root), self.recipient, rows[0].message.seq if rows else 0
        )
        return AssignedSourceWindow(cursor, rows, tuple(sent))

    def native_events(self, record, routes, project_events, references):
        entry = record.entry
        published = False
        routing = routes.get(entry.id)
        lookup = self.recipient.lookup
        events = super().native_events(record, routes, project_events, references)
        if entry.final_reply:
            if routing is not None and routing.publications:
                marks = ",".join("?" for _ in routing.publications)
                originals = tuple(self.rows(
                    f"w.seq IN ({marks})", tuple(ref.seq for ref in routing.publications),
                    limit=len(routing.publications),
                ))
                published = all(
                    any(original.message.reference == ref
                        and original.audience.sender_lookup == lookup for original in originals)
                    for ref in routing.publications
                )
            if not published and references:
                marks = ",".join("?" for _ in references)
                originals = tuple(self.rows(
                    f"w.seq IN ({marks})", tuple(ref.seq for ref in references), limit=len(references),
                ))
                published = all(
                    any(original.message.reference == ref
                        and original.audience.sender_lookup == lookup for original in originals)
                    for ref in references
                )

        result = []
        for event in events:
            if published and isinstance(event, AssistantTranscript):
                continue
            # An assigned request has one original wire record in this source.
            # Its native prompt is a consumer of that record, not another input.
            if isinstance(event, UserTranscript) and event.routed:
                requests = event.routing.requests
                marks = ",".join("?" for _ in requests)
                originals = tuple(self.rows(
                    f"w.seq IN ({marks})",
                    tuple(reference.seq for reference in requests),
                    limit=len(requests),
                ))
                remaining = tuple(
                    reference
                    for reference in requests
                    if not any(
                        original.message.reference == reference for original in originals
                    )
                )
                if not remaining:
                    continue
                event = replace(event, routing=TurnRouting(remaining, event.routing.reply))
            result.append(event)
        return tuple(result)

    def events(self, original):
        message = original.message
        if original.audience.sender_lookup == self.recipient.lookup:
            return (
                SentTranscript.from_message(message),
            )
        return (
            IncomingTranscript(
                message.body,
                timestamp=message.timestamp,
                source=message.reference,
                route=MessageRoute(message.sender, (message.target,)),
                routing=TurnRouting((message.reference,)),
            ),
        )


class LiveAssignedTranscriptSource(AssignedTranscriptSource):
    """Live publication evidence is additional to original wire assignments."""

    def reply_revision(self, records):
        return records.publication_revision(self.root, self.recipient)

    def native_records(self, fragments, routes, reader):
        """Native ancestry precedes one atomic SQL read per bounded fragment."""
        lookup = self.recipient.lookup
        for fragment in fragments:
            fragment = tuple(fragment)
            selected = tuple(zip(fragment, reader.inputs_for(fragment)))
            with NativeRuntimeInput.publication_source(self.root) as db:
                # Missing coordination remains detached native history. Do not
                # introduce header failures where no SQL source was selected.
                session_id = (reader.session_id
                              if db is not None and any(
                                  user.tracked_user
                                  for _, user in selected if user is not None) else None)
                capture = partial(
                    NativeRuntimeInput.transcript_projection,
                    db, reader, owner_lookup=lookup, session_id=session_id,
                )
                originals = tuple((record, record.project(partial(capture, user=user)))
                                  for record, user in selected)
            for record, projection in originals:
                yield record, record.project(
                    lambda record: self.native_events(record, routes, *projection)
                )
