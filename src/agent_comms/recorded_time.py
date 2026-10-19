"""Original recorded times, including explicitly unrecorded facts."""

from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass, replace
from math import isfinite
from typing import TYPE_CHECKING, ClassVar, TypeVar

from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .transcript_events import TranscriptEvent

Event = TypeVar("Event", bound="TranscriptEvent")


@dataclass(frozen=True)
class Lateness:
    """How late one delivery was against its deadline, to the second."""

    due: float
    delivered: float

    @property
    def seconds(self) -> float:
        return self.delivered - self.due

    def late(self) -> bool:
        return self.seconds > 0

    def __str__(self) -> str:
        due, delivered = (
            time.strftime("%H:%M:%S", time.localtime(at)) for at in (self.due, self.delivered)
        )
        return f"late by {self.seconds:g} s: due {due}, delivered {delivered}"


class EventTime(DeclaredFamily, affix="EventTime"):
    order_limit_minutes: ClassVar[int] = 15

    @classmethod
    def capture(cls, timestamp: float) -> EventTime:
        """Capture a recorded Unix-seconds fact, never the read time."""
        return RecordedEventTime(timestamp)

    def require_recorded(self, label: str) -> None:
        raise ValueError(f"{label} requires a recorded time.")

    def recorded_at(self, label: str) -> float:
        """The recorded instant; an unrecorded time refuses like ``require_recorded``."""
        raise ValueError(f"{label} requires a recorded time.")

    def lateness(self, deadline: EventTime) -> Lateness:
        """How late this delivery time is against ``deadline``; either unrecorded refuses."""
        return Lateness(deadline.recorded_at("An order deadline"), self.recorded_at("A delivery"))

    def require_deadline(self, ordered: EventTime) -> None:
        if (self.recorded_at("An order deadline") - ordered.recorded_at("An order")
                > self.order_limit_minutes * 60):
            raise ValueError(f"An order's deadline must be within {self.order_limit_minutes} minutes "
                             "of its recorded send time.")

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
