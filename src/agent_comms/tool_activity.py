"""Original tool facts and explicit selector availability, not a second selector."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .field_codec import projected
from .recorded_time import EventTime
from .transcript_events import ToolStartTranscript


class ToolActivity(DeclaredFamily, affix="ToolActivity"):
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
        """Only a proven source can establish the absence of a recent tool."""


@dataclass(frozen=True)
class NoToolActivity(ToolActivity):
    """The source selector proved that it contains no persisted tool call."""

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
        return EventTime.capture(self.call.timestamp).timestamps

    @property
    def description(self):
        return self.call.title

    def silent_since(self, busy_since, now, seconds):
        return EventTime.capture(self.call.timestamp).latest(busy_since).elapsed(now, seconds)


@dataclass(frozen=True)
class NativeToolSelectorUnavailable(ToolActivity):
    @property
    def description(self):
        return "Native exact tool selector is unavailable"

    def silent_since(self, busy_since, now, seconds):
        return False
