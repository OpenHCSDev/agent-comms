"""Last recorded tool facts, read from their original transcript owner."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from .declared_family import DeclaredFamily
from .transcript_events import ToolStartTranscript
from .recorded_time import EventTime, RecordedEventTime
from .field_codec import projected


class ToolActivity(DeclaredFamily, affix="ToolActivity"):
    @classmethod
    def from_events(cls, events):
        calls = tuple(call for event in events for call in event.tool_calls)
        return (RecordedToolActivity(max(calls, key=lambda call: call.timestamp or 0))
                if calls else NoToolActivity())

    @property
    def timestamps(self) -> tuple[float, ...]:
        return ()

    @projected(view="wire", name="detail")
    def display_detail(self) -> str:
        return self.description

    @property
    @abstractmethod
    def description(self) -> str: ...

    @abstractmethod
    def silent_since(self, busy_since: EventTime, now: float, seconds: float) -> bool:
        """Only a known source can establish the absence of a recent tool."""


@dataclass(frozen=True)
class NoToolActivity(ToolActivity):
    @property
    def description(self):
        return "No tool call recorded"

    def silent_since(self, busy_since, now, seconds):
        return busy_since.elapsed(now, seconds)


@dataclass(frozen=True)
class RecordedToolActivity(ToolActivity):
    call: ToolStartTranscript

    @property
    def timestamps(self):
        return (self.call.timestamp,) if self.call.timestamp is not None else ()

    @property
    def description(self):
        return self.call.title

    def silent_since(self, busy_since, now, seconds):
        # Missing external timestamps are unrecorded, never the time of this read.
        return (self.call.timestamp is not None
                and busy_since.latest(RecordedEventTime(self.call.timestamp)).elapsed(now, seconds))


@dataclass(frozen=True)
class NativeToolSelectorUnavailable(ToolActivity):

    @property
    def description(self):
        return "Native exact tool selector is not implemented"

    def silent_since(self, busy_since, now, seconds):
        return False
