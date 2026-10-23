from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO

from agent_comms.installer.interpreter_operation import RuntimeProbe


@dataclass(frozen=True)
class CliOutput:
    returncode: int
    stdout: str


@dataclass(frozen=True)
class CliCommandProbe(RuntimeProbe[CliOutput]):
    """One command-line call, answered by the selected runtime's own command line."""

    arguments: tuple[str, ...]

    def run(self) -> CliOutput:
        from agent_comms import cli

        with StringIO() as output, redirect_stdout(output):
            returncode = cli.main(self.arguments)
            return CliOutput(returncode, output.getvalue())
