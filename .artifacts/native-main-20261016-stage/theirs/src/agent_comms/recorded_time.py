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

    @abstractmethod
    def stamp(self, event: Event) -> Event: ...

    @property
    @abstractmethod
    def timestamps(self) -> tuple[float, ...]: ...

    @abstractmethod
    def since(self, at: float) -> bool: ...

    @abstractmethod
    def elapsed(self, now: float, seconds: float) -> bool: ...

    @abstractmethod
    def continued_from(self, original_at: float): ...

    @abstractmethod
    def latest(self, other): ...

    @abstractmethod
    def latest_known(self, at: float): ...


@dataclass(frozen=True)
class UnrecordedEventTime(EventTime):
    def stamp(self, event: Event) -> Event:
        return event

    @property
    def timestamps(self):
        return ()

    def since(self, at):
        return False

    def elapsed(self, now, seconds):
        return False

    def continued_from(self, original_at):
        return RecordedEventTime(original_at)

    def latest(self, other):
        return self

    def latest_known(self, at):
        return self


@dataclass(frozen=True)
class RecordedEventTime(EventTime):
    at: float

    def __post_init__(self):
        if type(self.at) not in (int, float) or not isfinite(self.at):
            raise ValueError("A recorded event time must be a finite Unix timestamp.")

    def stamp(self, event: Event) -> Event:
        return replace(event, timestamp=self.at)

    @property
    def timestamps(self):
        return (self.at,)

    def since(self, at):
        return self.at >= at

    def elapsed(self, now, seconds):
        return now - self.at >= seconds

    def continued_from(self, original_at):
        return self

    def latest(self, other):
        return other.latest_known(self.at)

    def latest_known(self, at):
        return type(self)(max(self.at, at))
