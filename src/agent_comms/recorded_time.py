"""Original recorded times, including explicitly unrecorded facts."""

from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass
from math import isfinite


from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, FieldRepresentation

class MissingTranscriptTimestampError(ValueError):
    """An external source cannot publish an event without its recorded time."""


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

    def since(self, timestamp: float) -> bool:
        return False

    def before(self, timestamp: float) -> bool:
        return False

    def clock(self) -> str:
        return "-"

    def require_recorded(self, label: str) -> None:
        raise ValueError(f"{label} requires a recorded time.")

    def recorded_at(self, label: str) -> float:
        """The recorded instant; an unrecorded time refuses like ``require_recorded``."""
        raise ValueError(f"{label} requires a recorded time.")

    def lateness(self, deadline: EventTime) -> Lateness:
        """How late this delivery time is against ``deadline``; either unrecorded refuses."""
        return Lateness(deadline.recorded_at("An order deadline"), self.recorded_at("A delivery"))

    def require_deadline(self) -> None:
        self.require_recorded("An order deadline")

    def require_delivery(self) -> None:
        self.require_recorded("A delivery")

    def transcript_timestamp(self) -> float:
        raise MissingTranscriptTimestampError("A transcript event requires its source's recorded time")

    @abstractmethod
    def continued_from(self, original_at: float): ...


@dataclass(frozen=True)
class UnrecordedEventTime(EventTime):
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

    def since(self, timestamp: float) -> bool:
        return self.at >= timestamp

    def before(self, timestamp: float) -> bool:
        return self.at < timestamp

    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.at))

    def recorded_at(self, label: str) -> float:
        return self.at

    def transcript_timestamp(self) -> float:
        return self.at

    def continued_from(self, original_at):
        return self
