"""Native child startup resources and readiness bounds."""

from __future__ import annotations

import getpass
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .diagnostics import PublicationMeasurements

if TYPE_CHECKING:
    from .native_pi import NativePiRpcLaunch


@dataclass(frozen=True)
class NativeStartupPolicy:
    # A cold extension can initialize a local stdio server before Pi answers get_state.
    readiness_seconds: float = 10.0
    readiness_step_bytes: int = 8 * 1024 * 1024
    readiness_step_seconds: float = 2.0
    readiness_max_seconds: float = 35.0

    def readiness_timeout(
        self, session_bytes: int | None, *, base_seconds: float | None = None
    ) -> float:
        """Bound get_state wait by saved-session size, not the model/turn clock."""
        base = self.readiness_seconds if base_seconds is None else base_seconds
        extra_steps = (
            max(0, (session_bytes - 1) // self.readiness_step_bytes)
            if session_bytes is not None and session_bytes > 0
            else 0
        )
        return min(self.readiness_max_seconds, base + extra_steps * self.readiness_step_seconds)


NATIVE_STARTUP_POLICY = NativeStartupPolicy()


class NativeStartup:
    """Directory and measurements shared while opening one native child."""

    def __init__(self, root: Path, *, measurements: PublicationMeasurements | None = None):
        self.root = root
        self.measurements = measurements if measurements is not None else PublicationMeasurements()

    @classmethod
    def for_launch(
        cls, launch: NativePiRpcLaunch, *, root: Path | None = None,
        measurements: PublicationMeasurements | None = None,
    ) -> NativeStartup:
        if root is not None:
            return cls(root, measurements=measurements)
        return cls(
            Path(
                launch.env.get("AGENT_COMMS_ROOT")
                or os.environ.get("AGENT_COMMS_ROOT")
                or str(Path(tempfile.gettempdir()) / f"agent-comms-startup-{getpass.getuser()}")
            ).expanduser(), measurements=measurements,
        )
