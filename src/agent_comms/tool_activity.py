"""Last recorded tool facts, read from their original transcript owner."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from .declared_family import DeclaredFamily
from .transcript_events import ToolStartTranscript


class ToolActivity(DeclaredFamily, affix="ToolActivity"):
    @classmethod
    def from_events(cls, events):
        calls = tuple(call for event in events for call in event.tool_calls)
        return (RecordedToolActivity(max(calls, key=lambda call: call.timestamp or 0))
                if calls else NoToolActivity())

    @abstractmethod
    def silent_since(self, busy_since: float, now: float, seconds: float) -> bool:
        """Only a known source can establish the absence of a recent tool."""


@dataclass(frozen=True)
class NoToolActivity(ToolActivity):
    def silent_since(self, busy_since, now, seconds):
        return now - busy_since >= seconds


@dataclass(frozen=True)
class RecordedToolActivity(ToolActivity):
    call: ToolStartTranscript

    def silent_since(self, busy_since, now, seconds):
        # Missing external timestamps are unrecorded, never the time of this read.
        return (self.call.timestamp is not None
                and now - max(busy_since, self.call.timestamp) >= seconds)


@dataclass(frozen=True)
class UnavailableToolActivity(ToolActivity):
    reason: str

    def silent_since(self, busy_since, now, seconds):
        return False
