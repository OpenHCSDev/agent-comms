"""Original recorded times, including explicitly unrecorded facts."""

from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass
from math import isfinite
from typing import ClassVar, Never

from .declared_family import DeclaredFamily
from .field_codec import AcceptsNull, FieldCodec, FieldRepresentation, NullMember, boundary_decoder

class RecordedEventSeconds(FieldRepresentation):
    """The recorded-time value of an existing numeric-seconds contract."""

    @classmethod
    def decode(cls, value: object) -> RecordedEventTime:
        return RecordedEventTime(FieldCodec.decode(float, value))

    @classmethod
    def encode(cls, value: object) -> float:
        return value.to_number(1)

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(float)


class NullableEventSeconds(AcceptsNull, FieldRepresentation):
    """A source clock may be explicitly unrecorded; units belong to its wire type."""

    units_per_second: ClassVar[int] = 1

    @classmethod
    @boundary_decoder
    def decode(cls, value: object) -> EventTime:
        timestamp = FieldCodec.decode(float | None, value)
        return (UnrecordedEventTime() if timestamp is None
                else RecordedEventTime(timestamp / cls.units_per_second))

    @classmethod
    def encode(cls, value: object) -> object:
        return value.to_number(cls.units_per_second)

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.alternatives_schema("anyOf", [
            FieldCodec.value_schema(float), NullMember.schema(),
        ])


class EventMilliseconds(NullableEventSeconds):
    """Pi message clocks use milliseconds; owned event time remains Unix seconds."""

    units_per_second = 1000


class UnrecordedTimeError(ValueError):
    """An operation requires a source-recorded time, but none was supplied."""


class GeneratedTimestamp(NullableEventSeconds):
    """Read a generated SQL timestamp as its recorded or unrecorded fact."""

    @classmethod
    def encode(cls, value: object) -> Never:
        raise TypeError("A generated timestamp is read from its event, never written")


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

    def require_delivery(self) -> None:
        self.require_recorded("A delivery")

    def latest_with(self, timestamp: float) -> float:
        """The later of this instant and `timestamp`; an unrecorded instant bounds nothing."""
        return timestamp

    @abstractmethod
    def to_number(self, units_per_second: int) -> object: ...

    @abstractmethod
    def continued_from(self, original_at: float): ...


@dataclass(frozen=True)
class UnrecordedEventTime(EventTime):
    def to_number(self, units_per_second: int) -> object:
        return None

    def continued_from(self, original_at):
        return RecordedEventTime(original_at)


@dataclass(frozen=True)
class RecordedEventTime(EventTime):
    at: float

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

    def to_number(self, units_per_second: int) -> float:
        return self.at * units_per_second

    def recorded_at(self, label: str) -> float:
        return self.at

    def latest_with(self, timestamp: float) -> float:
        return max(self.at, timestamp)

    def continued_from(self, original_at):
        return self
