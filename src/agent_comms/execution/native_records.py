from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Never

from agent_comms.errors import RelationViolationError
from agent_comms.execution.native_entries import (
    NativeEntry,
    NoEntryId,
    RecordedEntry,
    TranscriptProjection,
)
from agent_comms.execution.turn_item import FreshNativeId
from agent_comms.nominal.field_codec import MalformedRecord


class TranscriptRecord:
    """A source position; only native records can project a decoded native entry."""

    def project[Result](self, projection: Callable[[NativeRecord], Result]) -> Result | tuple[()]:
        """External records have events already, but no native entry for this callback."""
        return ()


@dataclass(frozen=True)
class NativeRecord(TranscriptRecord):
    start: int
    end: int
    entry: RecordedEntry | None
    complete: bool

    @classmethod
    def read(cls, start: int, end: int, raw: bytes):
        # Only a record still being appended lacks its newline; a complete
        # record that does not decode is corrupt history and raises.
        complete = raw.endswith(b"\n")
        entry = NativeEntry.read(raw) if complete else None
        return cls(start, end, entry, complete)

    def project(self, projection):
        """A decoded record owns whether it can produce presentation events."""
        return projection(self) if self.entry is not None else ()

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def annotation_id(self) -> FreshNativeId | NoEntryId:
        entry = self.entry
        return entry.annotation_id if entry is not None else NoEntryId()

    @property
    def events(self):
        return self.project(lambda original: original.entry.events(TranscriptProjection(position=original.start)))

    @property
    def input_reported(self) -> bool:
        return any(message.input_id.is_reported()
                   for message in self.project(lambda original: original.entry.messages))

    def require_entry(self) -> RecordedEntry:
        if not self.complete or self.entry is None:
            raise MalformedRecord("Native publication has incomplete evidence")
        return self.entry

    def is_final_publication(self) -> bool:
        entry = self.require_entry()
        if entry.final_reply:
            entry.require_entry_id()
            return True
        return False


class NoNativeInput(NativeRecord):
    """A parent chain that reached its root without an input record."""

    def __init__(self) -> None:
        super().__init__(0, 0, None, False)

    def require_entry(self) -> Never:
        raise RelationViolationError("Native publication has no tracked input ancestry")


class StoredRecord(NativeRecord):
    @property
    def size(self):
        return sum(event.text_size for event in self.events)
