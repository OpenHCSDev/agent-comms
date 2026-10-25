"""The canonical wire owns incoming and outgoing conversation history.

Native bytes and committed wire sequences are independent source coordinates.
A display cursor advances each only when that source's record is consumed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Self

from .native_entries import TranscriptProjection

if TYPE_CHECKING:
    from .context_store import ContextStore
    from .external_transcript import ExternalPageRow
    from .message_reference import MessageReference
    from .native_entries import TranscriptProjection
    from .native_transcript import NativeRecord, NativeTranscript
    from .threads import Thread, ThreadProvenance
    from .transcript_events import TextTranscript, TranscriptEvent
    from .transcript_routes import TranscriptRoutes, _SessionRoutes
    from .transcripts import NativeTranscriptCursor, TranscriptCursor
    from .turn_context import SegmentManifest
    from .turn_digests import TurnBound

from .bus import Bus
from .bus_publication import Delivered
from .coordination_errors import StaleRevision, TranscriptChanged
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, MalformedRecord
from .native_runtime_input import NativeRuntimeInput, PublishedReplyRevision
from .recorded_time import EventTime, UnrecordedEventTime
from .routing import TurnRouting
from .sent_message import PublishedSentMessage
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .transcript_events import (
    IncomingTranscript,
    SentTranscript,
    ToolEndTranscript,
    ToolStartTranscript,
    TurnDigest,
)
from .turn_item import NativeTurnId
from .typed_table import StoreWriter


@dataclass(frozen=True)
class AssignedSourceIdentity:
    root: str
    recipient: ThreadIncarnation

    @property
    def publication_key(self) -> tuple[str, float]:
        """Published authorship survives a rename, not a new birth or wire."""
        return self.root, self.recipient.require_recorded().created_at

    def same_publication_source(self, other: AssignedSourceIdentity) -> bool:
        return self.root == other.root and self.recipient == other.recipient


class WireFrontier(DeclaredFamily, affix="WireFrontier"):
    """The wire coordinate owns cursor construction, bounds and source authority."""

    sequence: int

    def cursor(self, session_file: str, offset: int) -> NativeTranscriptCursor:
        from .transcripts import NativeTranscriptCursor
        return NativeTranscriptCursor(session_file, offset)

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

    def resolve_sent_messages(
        self, reports: Iterable[SentTranscript],
    ) -> dict[int, SentTranscript | PublishedSentMessage]:
        """Index these exact reports using this source's publication evidence."""
        reports = tuple(reports)
        return dict(zip(map(id, reports), self.resolve_sent(reports), strict=True))


@dataclass(frozen=True)
class NoWireFrontier(WireFrontier):
    sequence = 0


@dataclass(frozen=True)
class AssignedSourceCursor(AssignedSourceIdentity, WireFrontier):
    sequence: int = field()

    def cursor(self, session_file: str, offset: int) -> NativeTranscriptCursor:
        from .transcripts import WireTranscriptCursor
        return WireTranscriptCursor(session_file, offset, self)

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

    def read_records(
        self,
        reader: TranscriptSource,
        project,
        sources: TranscriptSource,
        initial: TranscriptCursor,
        frontier: TranscriptCursor,
        *,
        max_messages: int,
        max_bytes: int,
    ) -> tuple[TranscriptCursor, list[tuple[TranscriptEvent, ...]]]:
        """Advance each file position only after reading the record at that position."""
        consumed, records, used = initial, [], 0
        # A tool round opened by an already appended record keeps the window open
        # until its paired record is appended too; the bounded grace keeps an
        # interrupted call (its result record never arrives) from growing a page.
        open_rounds: set[str] = set()
        round_grace = 0
        native = (self.native(reader, initial, frontier) if reader.has_records(frontier.offset)
                  else (record for record in ()))
        projected = project(native, max_records=max_messages + 1, max_bytes=max_bytes)
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
                batch = events if choose_native else sources.events(message)
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
                consumed = consumed.at_offset(frontier.offset if self.ascending else reader.start)
            if message is None:
                consumed = consumed.at_sequence(frontier.wire.sequence if self.ascending else 0)
            return consumed, records
        finally:
            projected.close()
            native.close()

    def request_earlier(self, records: TranscriptSource, offset: int) -> bool:
        return records.request_earlier(offset)

    ascending = False
    # Reading newest-first meets a round's result before its call.
    descending_rounds = True
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

    ascending = True
    descending_rounds = False

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


class TranscriptDraft:
    def end_turn(self, transcript: StoreWriter) -> None:
        """An absent draft needs no write."""

    def publish(self, transcript: StoreWriter) -> None:
        """An absent draft needs no publication."""

    def external_row(self):
        """The ACP draft field is null when there is no external draft."""
        return None


@dataclass(frozen=True)
class NoDraft(TranscriptDraft):
    pass


class TranscriptRecord:
    """A source position; only native records can project a decoded native entry."""

    def project[Result](self, projection: Callable[[NativeRecord], Result]) -> Result | tuple[()]:
        """External records have events already, but no native entry for this callback."""
        return ()


class TranscriptSource:
    """Recorded items alone carry no assigned-wire or publication authority.

    Native and external stores supply their original records. An assigned
    source adds the separately acquired wire/publication capability below.
    """

    start = 0

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
        self, contexts: ContextStore, entry_ids: tuple[str, ...],
    ) -> dict[str, SegmentManifest]:
        """Only a native journal records a context manifest per input entry."""
        return {}

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
        from .external_transcript import DraftRow
        with self.draft_snapshot(root, incarnation) as connection:
            return DraftRow.one(connection, thread=incarnation.require_recorded().lookup) or NoDraft()

    def draft_snapshot(self, root: Path, incarnation: ThreadIncarnation):
        from .external_transcript import ExternalTranscript
        return ExternalTranscript(root, incarnation).snapshot()

    def publication_revision(self, root: Path, recipient: RecordedThreadIncarnation):
        return PublishedReplyRevision(0, 0)

    @contextmanager
    def projection(self, source: TranscriptSource, routes: TranscriptRoutes):
        def project(records, *, max_records: int, max_bytes: int):
            return ((record, tuple(retained for event in record.events
                                   for retained in event.for_external_source(source)))
                    for record in records)
        yield project

    def sent_messages(self, references: Iterable[MessageReference]) -> dict[MessageReference, Delivered]:
        return {}

    def resolve_sent(self, messages: tuple[SentTranscript, ...]):
        """Records without a sender assignment cannot qualify Send reports."""
        return self.frontier.resolve_sent(messages)

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
        reader: NativeTranscript,
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


@dataclass(frozen=True)
class AssignedTranscriptSource(TranscriptSource):
    root: Path
    recipient: RecordedThreadIncarnation
    bus: Bus

    @classmethod
    def for_thread(cls, root: Path, thread: Thread, bus: Bus) -> Self:
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

    def sent_messages(self, references: Iterable[MessageReference]) -> dict[MessageReference, Delivered]:
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
    def for_recorded(cls, root: Path, thread: ThreadProvenance, bus: Bus, records: TranscriptSource):
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
                     for retained in event.for_assigned_source(self, published))

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
        reader: NativeTranscript,
    ) -> Iterator[tuple[NativeRecord, tuple[TranscriptEvent, ...]]]:
        """Native ancestry precedes one atomic SQL read per bounded fragment."""
        from .transcripts import TranscriptRead

        lookup = self.recipient.lookup
        for fragment in fragments:
            fragment = tuple(fragment)
            selected = tuple(
                (record, user.entry if user else None)
                for record, user in zip(fragment, reader.inputs_for(fragment), strict=True)
            )
            with TranscriptRead.publication_source(self.root) as db:
                # Missing coordination remains detached native history. Do not
                # introduce header failures where no SQL source was selected.
                session_id = (reader.session_id
                              if db is not None and any(
                                  user and user.message.input_id.reported
                                  for _, user in selected) else None)
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
