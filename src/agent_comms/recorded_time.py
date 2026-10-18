"""Original recorded times, including explicitly unrecorded facts."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from math import isfinite
from typing import TYPE_CHECKING, TypeVar

from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .transcript_events import TranscriptEvent

Event = TypeVar("Event", bound="TranscriptEvent")


class EventTime(DeclaredFamily, affix="EventTime"):
    @classmethod
    def capture(cls, timestamp: float) -> EventTime:
        """Capture a recorded Unix-seconds fact, never the read time."""
        return RecordedEventTime(timestamp)

    def require_recorded(self, label: str) -> None:
        raise ValueError(f"{label} requires a recorded time.")

    def recorded_at(self, label: str) -> float:
        """The recorded instant; an unrecorded time refuses like ``require_recorded``."""
        raise ValueError(f"{label} requires a recorded time.")

    def require_deadline(self) -> None:
        self.require_recorded("An order deadline")

    def require_delivery(self) -> None:
        self.require_recorded("A delivery")

    @abstractmethod
    def stamp(self, event: Event) -> Event: ...

    @abstractmethod
    def continued_from(self, original_at: float): ...


@dataclass(frozen=True)
class UnrecordedEventTime(EventTime):
    def stamp(self, event: Event) -> Event:
        return event

    def continued_from(self, original_at):
        return RecordedEventTime(original_at)


@dataclass(frozen=True)
class RecordedEventTime(EventTime):
    at: float

    def __post_init__(self):
        if type(self.at) not in (int, float) or not isfinite(self.at):
            raise ValueError("A recorded event time must be a finite Unix timestamp.")

    def require_recorded(self, label: str) -> None:
        pass

    def recorded_at(self, label: str) -> float:
        return self.at

    def stamp(self, event: Event) -> Event:
        return replace(event, timestamp=self.at)

    def continued_from(self, original_at):
        return self
