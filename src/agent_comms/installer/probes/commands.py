from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

from agent_comms.installer.interpreter_operation import CliProbePhase, RuntimeProbe


@dataclass(frozen=True)
class CliOutput:
    returncode: int
    stdout: str


@dataclass(frozen=True)
class CliCommandProbe(RuntimeProbe[CliOutput]):
    """One command-line call, answered by the selected runtime's own command line.

    The worker overlays the target's installer package on this runtime, so Core
    modules imported here that import the installer would get the target's copy
    beside their own runtime's Core. The command line therefore runs in its own
    process of this interpreter, which imports this runtime alone."""

    arguments: tuple[str, ...]

    def install_phase(self) -> CliProbePhase:
        return CliProbePhase(probe=type(self), arguments=self.arguments)

    def run(self) -> CliOutput:
        completed = subprocess.run(
            (sys.executable, "-m", "agent_comms.cli", *self.arguments),
            stdout=subprocess.PIPE,
            text=True,
            check=False,
        )
        return CliOutput(completed.returncode, completed.stdout)
