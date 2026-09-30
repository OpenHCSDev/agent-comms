"""The canonical wire owns incoming and outgoing conversation history.

Native bytes and committed wire sequences are independent source coordinates.
A display cursor advances each only when that source's record is consumed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
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

    ascending = False


class EarlierTranscript(TranscriptTraversal):
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

    def native_events(self, record, routes, reader):
        from dataclasses import replace
        from .native_entries import TranscriptProjection

        entry = record.entry
        published = False
        routing = routes.get(entry.id)
        if entry.final_reply:
            from .native_runtime_input import NativeRuntimeInput

            lookup = stable_thread_lookup(self.recipient.created_at)
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
            user = reader.input_ancestor(record) if not published else None
            if user is not None:
                reference = NativeRuntimeInput.published_reply(self.root, reader, user, lookup)
                if reference is not None:
                    originals = self.rows("w.seq=?", (reference.seq,))
                    published = bool(originals) and (
                        originals[0].message.reference == reference
                        and originals[0].audience.sender_lookup == lookup
                    )
        events = entry.events(
            TranscriptProjection(
                routing,
                routes.input_display(entry.input_id),
            )
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
