"""Original recorded times, including explicitly unrecorded facts."""

from abc import abstractmethod
from dataclasses import dataclass
from math import isfinite

from .declared_family import DeclaredFamily


class EventTime(DeclaredFamily, affix="EventTime"):
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
