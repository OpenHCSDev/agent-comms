"""The install's last step, run by the runtime that now owns the bus.

The installer process runs the outgoing runtime, and a root admits only its recorded
owner, so registering harnesses on the carried bus is a probe in the target's runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_commands import InstallCliCommand, InstallResult
from agent_comms.field_codec import PathText
from agent_comms.installer.interpreter_operation import RuntimeProbe


@dataclass(frozen=True)
class HarnessesProbe(RuntimeProbe[InstallResult]):
    root: Annotated[Path, PathText]
    install: InstallCliCommand

    def run(self) -> InstallResult:
        from agent_comms.comms import client_wire

        with client_wire(self.root) as comms:
            return self.install.apply(comms)


if __name__ == "__main__":
    RuntimeProbe.main()
