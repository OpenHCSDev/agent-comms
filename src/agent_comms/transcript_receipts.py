"""The assignment ledger owns inbound history, including passive receipts.

Native bytes and committed wire sequences are independent source coordinates.
A display cursor advances each only when that source's record is consumed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .bus_publication import stable_thread_lookup
from .notification_assignment import NotificationAssignment
from .routing import TurnRouting
from .thread_identity import ThreadIncarnation
from .transcript_events import UserTranscript

if TYPE_CHECKING:
    from .wire_log import WireLog


@dataclass(frozen=True)
class AssignedSourceCursor:
    root: str
    recipient: ThreadIncarnation
    sequence: int

    def contains(self, other: AssignedSourceCursor) -> bool:
        return (self.root == other.root and self.recipient == other.recipient
                and self.sequence >= other.sequence)

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
        return "w.wire_seq<=?", (sequence,)

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
        return "w.wire_seq>? AND w.wire_seq<=?", (sequence, through.wire_seq)

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
        return NotificationAssignment.select(
            self.root, f"w.recipient_lookup=? AND ({predicate})",
            (stable_thread_lookup(self.recipient.created_at), *parameters), limit=limit, ascending=ascending,
        )

    @property
    def frontier(self):
        rows = self.rows()
        return AssignedSourceCursor(str(self.root), self.recipient,
                                    rows[0].assignment.wire_seq if rows else 0)

    def owns(self, sequence):
        return bool(self.rows("w.wire_seq=?", (sequence,)))

    def next(self, traversal, sequence, through):
        predicate, parameters = traversal.predicate(sequence, through)
        rows = self.rows(predicate, parameters, ascending=traversal.ascending)
        if not rows:
            return None
        source = rows[0].assignment.source
        message = self.log.message_by_id(source.message_id)
        if message is None or message.seq != source.seq:
            raise ValueError("Assigned transcript source has no matching durable wire message")
        return message

    def native_events(self, entry, routes, messaging):
        from dataclasses import replace
        from .native_entries import TranscriptProjection

        events = entry.events(TranscriptProjection(
            routes.get(entry.id), routes.input_display(entry.input_id),
            messaging.sent_tool_message,
        ))
        result = []
        for event in events:
            # An assigned request has one original wire record in this source.
            # Its native prompt is a consumer of that record, not another input.
            if isinstance(event, UserTranscript) and event.routed:
                remaining = tuple(message for message in event.routing.requests
                                  if not self.owns(message.seq))
                if not remaining:
                    continue
                event = replace(event, routing=TurnRouting(remaining, event.routing.reply))
            result.append(event)
        return tuple(result)

    @staticmethod
    def events(message):
        return (UserTranscript(message.body, timestamp=message.timestamp,
                               routing=TurnRouting((message,))),)
