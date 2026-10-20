"""Original recorded times, including explicitly unrecorded facts."""

from __future__ import annotations

import re
import time
from abc import abstractmethod
from dataclasses import dataclass
from math import isfinite
from typing import cast

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, FieldRepresentation

class UnrecordedTimeError(ValueError):
    """An operation requires a source-recorded time, but none was supplied."""


class GeneratedTimestamp(FieldRepresentation):
    """Read a generated SQL timestamp as its recorded or unrecorded fact."""

    accepts_null = True

    @classmethod
    def decode(cls, value: object) -> EventTime:
        timestamp = FieldCodec.decode(float | None, value)
        return UnrecordedEventTime() if timestamp is None else RecordedEventTime(timestamp)

    @classmethod
    def encode(cls, value: object) -> object:
        raise TypeError("A generated timestamp is read from its event, never written")


class EventTimeWire(GeneratedTimestamp):
    """Public timestamp scalars retain the existing recorded/absent owner."""

    @classmethod
    def encode(cls, value: object) -> object:
        return cast(EventTime, value).wire_value()

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.alternatives_schema("anyOf", [
            FieldCodec.value_schema(float), FieldCodec.value_schema(type(None)),
        ])


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
    @classmethod
    def capture(cls, timestamp: float) -> EventTime:
        """Capture a recorded Unix-seconds fact, never the read time."""
        return RecordedEventTime(timestamp)

    def chronology(self) -> tuple[int, float]:
        """Undated facts sort after recorded events, without inventing a timestamp."""
        return (1, 0.0)

    def since(self, timestamp: float) -> bool:
        return False

    def before(self, timestamp: float) -> bool:
        return False

    def clock(self) -> str:
        return "-"

    def require_recorded(self, label: str) -> None:
        self.recorded_at(label)

    def recorded_at(self, label: str) -> float:
        """The recorded instant; callers requiring a time cannot invent one."""
        raise UnrecordedTimeError(f"{label} requires a recorded time.")

    def lateness(self, deadline: EventTime) -> Lateness:
        """How late this delivery time is against ``deadline``; either unrecorded refuses."""
        return Lateness(deadline.recorded_at("An order deadline"), self.recorded_at("A delivery"))

    def require_deadline(self) -> None:
        self.require_recorded("An order deadline")

    def require_delivery(self) -> None:
        self.require_recorded("A delivery")

    @abstractmethod
    def wire_value(self) -> object: ...

    @abstractmethod
    def continued_from(self, original_at: float): ...


@dataclass(frozen=True)
class UnrecordedEventTime(EventTime):
    def wire_value(self) -> object:
        return None

    def continued_from(self, original_at):
        return RecordedEventTime(original_at)


@dataclass(frozen=True)
class RecordedEventTime(EventTime):
    at: float

    def wire_value(self) -> object:
        return self.at

    def __post_init__(self):
        if type(self.at) not in (int, float) or not isfinite(self.at):
            raise ValueError("A recorded event time must be a finite Unix timestamp.")

    def chronology(self) -> tuple[int, float]:
        return (0, self.at)

    def since(self, timestamp: float) -> bool:
        return self.at >= timestamp

    def before(self, timestamp: float) -> bool:
        return self.at < timestamp

    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.at))

    @classmethod
    def from_clock(cls, clock: str) -> RecordedEventTime:
        """Decode an explicit local clock at the input boundary."""
        return cls(time.time()).at_clock(clock)

    def at_clock(self, clock: str) -> RecordedEventTime:
        """The next local HH:MM minute, anchored to this recorded instant."""
        if not re.fullmatch(r'(?:[01]?\d|2[0-3]):[0-5]\d', clock):
            raise ValueError('supply HH:MM local time (00:00 through 23:59)')
        hour, minute = map(int, clock.split(':'))
        day = time.localtime(self.at)
        at = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, hour, minute, 0, 0, 0, -1))
        return RecordedEventTime(at + 86400 if at < self.at - 60 else at)

    def recorded_at(self, label: str) -> float:
        return self.at

    def continued_from(self, original_at):
        return self
