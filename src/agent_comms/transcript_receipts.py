"""The canonical wire owns incoming and outgoing conversation history.

Native bytes and committed wire sequences are independent source coordinates.
A display cursor advances each only when that source's record is consumed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from .bus_publication import stable_thread_lookup
from .routing import MessageRoute, TurnRouting
from .thread_identity import ThreadIncarnation
from .transcript_events import AssistantTranscript, IncomingTranscript, SentTranscript, UserTranscript

if TYPE_CHECKING:
    from .wire_log import WireLog


@dataclass(frozen=True)
class AssignedSourceIdentity:
    root: str
    recipient: ThreadIncarnation


@dataclass(frozen=True)
class AssignedSourceCursor:
    root: str
    recipient: ThreadIncarnation
    sequence: int

    @property
    def identity(self) -> AssignedSourceIdentity:
        return AssignedSourceIdentity(self.root, self.recipient)

    def contains(self, other: AssignedSourceCursor) -> bool:
        return self.identity == other.identity and self.sequence >= other.sequence

    def require_source(self, identity: AssignedSourceIdentity) -> None:
        if self.identity != identity:
            raise ValueError("Receipt cursor belongs to a different transcript source")

    def covers(self, sequence: int) -> bool:
        return 0 < sequence <= self.sequence

    def at(self, sequence: int) -> AssignedSourceCursor:
        return replace(self, sequence=sequence)


class TranscriptTraversal(ABC):
    @abstractmethod
    def native(self, reader, cursor, through): ...

    @abstractmethod
    def predicate(self, sequence, through): ...

    @abstractmethod
    def chooses_native(self, native_time, receipt_time): ...

    @abstractmethod
    def native_position(self, record): ...

    @abstractmethod
    def receipt_position(self, message): ...

    @abstractmethod
    def finish(self, records): ...

    @abstractmethod
    def bounds(self, initial, consumed): ...

    @abstractmethod
    def chooses_outcome(self, record, outcome): ...

    @abstractmethod
    def outcome_position(self, outcome): ...

    @abstractmethod
    def outcome_rows(self, rows, sequence, through): ...

    def read_records(self, reader, project_native, receipts, outcomes, initial, frontier,
                     *, max_messages, max_bytes):
        """Consume each original source coordinate only when its record is read."""
        consumed, records, used = initial, [], 0
        native = self.native(reader, initial, frontier) if frontier.offset else (record for record in ())
        projected = project_native(reader.fragments(
            native, max_records=max_messages + 1, max_bytes=max_bytes,
        ))
        try:
            record, events = next(projected, (None, ()))
            source_rows = iter(
                receipts.page_rows(
                    self, consumed.wire_seq, frontier, limit=max_messages + 1
                )
            )
            message = next(source_rows, None)
            outcome_rows = iter(self.outcome_rows(
                outcomes, initial.outcome_seq, frontier,
            ))
            outcome = next(outcome_rows, None)
            while record is not None or message is not None or outcome is not None:
                if record is not None and not events:
                    if self.ascending and record.incomplete_tail(frontier.offset):
                        break
                    consumed = consumed.at_offset(self.native_position(record))
                    record, events = next(projected, (None, ()))
                    continue
                # NativeEntry owns one clock for all parts of a record.
                choose_outcome = outcome is not None and self.chooses_outcome(record, outcome)
                source_events = (outcome.event(),) if choose_outcome else events
                native_time = source_events[0].timestamp if source_events else None
                choose_native = record is not None and (
                    message is None
                    or self.chooses_native(native_time, message.message.timestamp)
                )
                choose_source = choose_outcome or choose_native
                batch = source_events if choose_source else receipts.events(message)
                cost = sum(event.text_size for event in batch)
                if choose_native and not choose_outcome:
                    cost = record.size
                if records and (len(records) >= max_messages or used + cost > max_bytes):
                    break
                records.append(batch)
                used += cost
                if choose_outcome:
                    consumed = consumed.at_outcome(self.outcome_position(outcome))
                    outcome = next(outcome_rows, None)
                elif choose_native:
                    consumed = consumed.at_offset(self.native_position(record))
                    record, events = next(projected, (None, ()))
                else:
                    consumed = consumed.at_sequence(self.receipt_position(message.message))
                    message = next(source_rows, None)
            if record is None:
                consumed = consumed.at_offset(frontier.offset if self.ascending else 0)
            if message is None:
                consumed = consumed.at_sequence(frontier.wire_seq if self.ascending else 0)
            if outcome is None:
                consumed = consumed.at_outcome(frontier.outcome_seq if self.ascending else 0)
            return consumed, records
        finally:
            projected.close()
            native.close()

    ascending = False


class EarlierTranscript(TranscriptTraversal):
    def chooses_outcome(self, record, outcome):
        return record is None or record.end <= outcome.native_offset

    def outcome_position(self, outcome):
        return outcome.sequence - 1

    def outcome_rows(self, rows, sequence, through):
        return (row for row in reversed(rows) if row.sequence <= sequence)

    def native(self, reader, cursor, through):
        return reader.reverse(cursor.offset)

    def predicate(self, sequence, through):
        return "w.seq<=?", (sequence,)

    def chooses_native(self, native_time, receipt_time):
        return native_time is None or native_time >= receipt_time

    def native_position(self, record):
        return record.start

    def receipt_position(self, message):
        return message.seq - 1

    def finish(self, records):
        return tuple(event for record in reversed(records) for event in record)

    def bounds(self, initial, consumed):
        return consumed, initial


class LaterTranscript(TranscriptTraversal):
    ascending = True

    def chooses_outcome(self, record, outcome):
        return record is None or record.start >= outcome.native_offset

    def outcome_position(self, outcome):
        return outcome.sequence

    def outcome_rows(self, rows, sequence, through):
        return (row for row in rows if sequence < row.sequence <= through.outcome_seq)

    def native(self, reader, cursor, through):
        return reader.forward(cursor.offset, through.offset)

    def predicate(self, sequence, through):
        return "w.seq>? AND w.seq<=?", (sequence, through.wire_seq)

    def chooses_native(self, native_time, receipt_time):
        return native_time is None or native_time <= receipt_time

    def native_position(self, record):
        return record.end

    def receipt_position(self, message):
        return message.seq

    def finish(self, records):
        return tuple(event for record in records for event in record)

    def bounds(self, initial, consumed):
        return initial, consumed


@dataclass(frozen=True)
class AssignedTranscriptSource:
    root: Path
    recipient: ThreadIncarnation
    log: WireLog

    @classmethod
    def for_thread(cls, root, thread, log):
        return cls(root, thread.incarnation, log)

    def rows(self, predicate="1", parameters=(), *, limit=1, ascending=False):
        return self.log.conversation_sources(
            stable_thread_lookup(self.recipient.created_at),
            predicate,
            parameters,
            limit=limit,
            ascending=ascending,
        )

    @property
    def frontier(self):
        return self.window()[0]

    def window(self, *, limit=1):
        """One certified original window owns both rows and its frontier."""
        rows = self.rows(limit=limit)
        return AssignedSourceCursor(
            str(self.root), self.recipient, rows[0].message.seq if rows else 0
        ), rows

    def page_rows(self, traversal, sequence, through, *, limit):
        predicate, parameters = traversal.predicate(sequence, through)
        return self.rows(predicate, parameters, ascending=traversal.ascending, limit=limit)

    def native_records(self, fragments, routes, reader):
        """One original SQL read per bounded fragment, closed before wire/render."""
        from .native_runtime_input import NativeRuntimeInput

        lookup = stable_thread_lookup(self.recipient.created_at)
        for fragment in fragments:
            with NativeRuntimeInput._publication_read(self.root) as db:
                capture = partial(
                    NativeRuntimeInput.transcript_projection,
                    db, reader, owner_lookup=lookup,
                )
                originals = tuple((record, record.project(capture)) for record in fragment)
            for record, projection in originals:
                yield record, record.project(
                    lambda record: self.native_events(record, routes, *projection)
                )

    def native_events(self, record, routes, project_events, references):
        from .native_entries import TranscriptProjection

        entry = record.entry
        published = False
        routing = routes.get(entry.id)
        lookup = stable_thread_lookup(self.recipient.created_at)
        events = project_events(TranscriptProjection(routing, routes.input_display(entry.input_id)))
        if entry.final_reply:
            if routing is not None and routing.publications:
                marks = ",".join("?" for _ in routing.publications)
                originals = self.rows(
                    f"w.seq IN ({marks})", tuple(ref.seq for ref in routing.publications),
                    limit=len(routing.publications),
                )
                published = all(
                    any(original.message.reference == ref
                        and original.audience.sender_lookup == lookup for original in originals)
                    for ref in routing.publications
                )
            if not published and references:
                marks = ",".join("?" for _ in references)
                originals = self.rows(
                    f"w.seq IN ({marks})", tuple(ref.seq for ref in references), limit=len(references),
                )
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
                originals = self.rows(
                    f"w.seq IN ({marks})",
                    tuple(reference.seq for reference in requests),
                    limit=len(requests),
                )
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
        if original.audience.sender_lookup == stable_thread_lookup(self.recipient.created_at):
            return (
                SentTranscript(
                    message.body,
                    timestamp=message.timestamp,
                    source=message.reference,
                    routing=TurnRouting(reply=MessageRoute(message.sender, (message.target,))),
                ),
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
