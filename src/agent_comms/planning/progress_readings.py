from __future__ import annotations

from abc import abstractmethod
from dataclasses import (
    dataclass,
    field,
)
from typing import (
    Annotated,
    ClassVar,
)

from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms.execution.child_process import CheckPrinted, CountPrinted, NoIntegerPrinted
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import DisclosedRecord, DisclosureIdentity
from agent_comms.storage.recorded_time import RecordedEventTime


@dataclass(frozen=True)
class ProgressPoint(DisclosedRecord):
    """A revision and its time in the order's progress; Git rows use committer time."""
    sha: Annotated[str, DisclosureIdentity()]
    at: float
    format: ClassVar[str] = "%H %ct"

    @classmethod
    def listed(cls, line: str) -> ProgressPoint:
        sha, at = line.split()
        return cls(sha, float(at))


@dataclass(frozen=True)
class DistanceReading(DisclosedRecord):
    at: float
    distance: CheckPrinted
    command: str = ""


class _LatestReadingTrend(MroDispatch):
    @handles(CountPrinted)
    def counted(self, printed: CountPrinted, readings: DistanceReadings) -> DistanceTrend:
        return readings.counted_trend(printed.count)

    @handles(NoIntegerPrinted)
    def uncounted(self, printed: NoIntegerPrinted, readings: DistanceReadings) -> DistanceTrend:
        return UncountedTrend(printed, readings.readings[-1].command)


class _PreviousReadingTrend(MroDispatch):
    @handles(CountPrinted)
    def counted(
        self, printed: CountPrinted, final: int, first_at: float, latest_at: float,
    ) -> DistanceTrend:
        if final >= printed.count:
            return FlatTrend(final, latest_at)
        rate = (printed.count - final) / max(latest_at - first_at, 1.0)
        return MovingTrend(final, latest_at + final / rate)

    @handles(NoIntegerPrinted)
    def uncounted(
        self, printed: NoIntegerPrinted, final: int, first_at: float, latest_at: float,
    ) -> DistanceTrend:
        return UnreadTrend()


_LATEST_READING_TREND = _LatestReadingTrend()
_PREVIOUS_READING_TREND = _PreviousReadingTrend()


@dataclass(frozen=True)
class DistanceReadings(DisclosedRecord):
    """The order's last two distance readings, oldest first: its base at the order, then its
    newest commit. Fewer than two read, or a last reading at done, project nothing."""
    readings: tuple[DistanceReading, ...] = ()
    window: ClassVar[int] = 2

    @property
    def trend(self) -> DistanceTrend:
        """A missing count diagnoses the check; only measured counts project a finish."""
        if not self.readings:
            return UnreadTrend()
        latest = self.readings[-1].distance
        return _LatestReadingTrend.handler_for(type(latest))(_LATEST_READING_TREND, latest, self)

    def counted_trend(self, final: int) -> DistanceTrend:
        last = self.readings[-self.window:]
        if len(last) < self.window or not final:
            return UnreadTrend()
        first, latest = last
        return _PreviousReadingTrend.handler_for(type(first.distance))(
            _PREVIOUS_READING_TREND, first.distance, final, first.at, latest.at,
        )


class DistanceTrend(DeclaredFamily, affix="Trend"):
    """What the order's distance readings say about its finish."""

    @property
    @abstractmethod
    def projection(self) -> str: ...


@dataclass(frozen=True)
class MovingTrend(DistanceTrend):
    distance: int
    done: float

    @property
    def projection(self) -> str:
        return f"; distance {self.distance}, projected done {RecordedEventTime(self.done).clock()}"


@dataclass(frozen=True)
class FlatTrend(DistanceTrend):
    """The distance did not fall from the reading before ``since``."""

    distance: int
    since: float

    @property
    def projection(self) -> str:
        return f"; distance {self.distance}, not falling"


@dataclass(frozen=True)
class UncountedTrend(DistanceTrend):
    printed: NoIntegerPrinted
    command: str

    @property
    def projection(self) -> str:
        return self.printed.progress(self.command)


@dataclass(frozen=True)
class UnreadTrend(DistanceTrend):
    """Fewer than two readings, or the last one at done: nothing to project from."""

    @property
    def projection(self) -> str:
        return ""


@dataclass(frozen=True)
class Moving:
    """Work whose last edit, commit, push or PR change was observed at ``at``."""
    at: float = field(kw_only=True)
    readings: DistanceReadings = field(default=DistanceReadings(), kw_only=True, compare=False)

    def distance_readings(self) -> DistanceReadings:
        return self.readings
