"""Native child startup resources and readiness bounds."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .diagnostics import PublicationMeasurements

if TYPE_CHECKING:
    from .native_pi import NativePiRpcLaunch


class SessionLoad(ABC):
    """The observed session size owns its startup allowance and diagnostics."""

    @property
    @abstractmethod
    def diagnostic(self) -> dict[str, int]: ...

    @property
    @abstractmethod
    def summary(self) -> str: ...

    @abstractmethod
    def extra_steps(self, step_bytes: int) -> int: ...

    @classmethod
    def capture(cls, session_file: str | None) -> SessionLoad:
        if session_file is None:
            return UnmeasuredSessionLoad()
        try:
            return MeasuredSessionLoad(Path(session_file).stat().st_size)
        except FileNotFoundError:
            return UnmeasuredSessionLoad()


class UnmeasuredSessionLoad(SessionLoad):
    summary = "unknown"

    @property
    def diagnostic(self) -> dict[str, int]:
        return {}

    def extra_steps(self, step_bytes: int) -> int:
        return 0


@dataclass(frozen=True)
class MeasuredSessionLoad(SessionLoad):
    size: int

    @property
    def diagnostic(self) -> dict[str, int]:
        return {"session_bytes": self.size}

    @property
    def summary(self) -> str:
        return str(self.size)

    def extra_steps(self, step_bytes: int) -> int:
        return max(0, (self.size - 1) // step_bytes)


@dataclass(frozen=True)
class NativeStartupPolicy:
    # A cold extension can initialize a local stdio server before Pi answers get_state.
    readiness_seconds: float = 10.0
    readiness_step_bytes: int = 8 * 1024 * 1024
    readiness_step_seconds: float = 2.0
    readiness_max_seconds: float = 35.0

    def readiness_timeout(
        self, session_load: SessionLoad, *, base_seconds: float | None = None
    ) -> float:
        """Bound get_state wait by saved-session size, not the model/turn clock.

        A cold Pi parses the entire saved JSONL before responding. Allow two
        extra seconds for each full 8 MiB beyond the first 8 MiB, up to 35s.
        Missing/unreadable sessions retain the ten-second base bound.
        """
        base = self.readiness_seconds if base_seconds is None else base_seconds
        extra_steps = session_load.extra_steps(self.readiness_step_bytes)
        return min(self.readiness_max_seconds, base + extra_steps * self.readiness_step_seconds)


NATIVE_STARTUP_POLICY = NativeStartupPolicy()


@dataclass
class NativeStartup:
    """Directory and measurements shared while opening one native child."""

    root: Path
    measurements: PublicationMeasurements = field(default_factory=PublicationMeasurements, kw_only=True)

    @classmethod
    def for_launch(
        cls, launch: NativePiRpcLaunch,
    ) -> NativeStartup:
        return cls(launch.root)
