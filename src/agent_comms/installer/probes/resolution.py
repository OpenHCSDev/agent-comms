"""One target-owned observation of package inputs and the interpreter running them."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_comms.installer.interpreter_operation import RuntimeProbe, TargetProbe
from agent_comms.installer.probes.interpreter import InterpreterBuildProbe
from agent_comms.installer.probes.packages import WheelPackagesProbe
from agent_comms.installer.runtime_resolution import RuntimeResolution


@dataclass(frozen=True)
class RuntimeResolutionProbe(TargetProbe, RuntimeProbe[RuntimeResolution]):
    core: str
    wheels: tuple[Path, ...] = ()

    def run(self) -> RuntimeResolution:
        return RuntimeResolution(
            core_commit=self.core,
            packages=WheelPackagesProbe(self.core, self.wheels).run(),
            interpreter=InterpreterBuildProbe().run(),
        )
