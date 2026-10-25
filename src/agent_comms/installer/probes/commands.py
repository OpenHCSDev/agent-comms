from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from agent_comms.cli_protocol import CoreError
from agent_comms.cli_result import CliResult
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.installer.interpreter_operation import CliProbePhase, RuntimeProbe

CORE_ERROR = TypeAdapter(CoreError.declared_union())


class CliRefusedError(Exception):
    """A command line that answered with Core's declared error instead of its result."""

    def __init__(self, failure: CoreError):
        super().__init__(failure.error)
        self.failure = failure


class CliUndecodableError(Exception):
    """A command line whose output is neither its declared result nor Core's error: it
    died before answering, and only its own error output says why."""


@dataclass(frozen=True)
class CliOutput:
    """What a command line printed. The worker encodes it on the runtime that ran the
    command (an older Core than the installer's, during an upgrade), so it stays plain
    text; the installer decodes it into the command's answer here, once."""

    returncode: int
    stdout: str
    stderr: str

    def answer[Answer: CliResult](self, result: type[Answer]) -> Answer:
        """The declared result, whose own exit code agrees with the process's (a batch
        reports its failed targets with a nonzero exit), or Core's error raised."""
        answer = self.decoded(result)
        if answer.exit_code() != self.returncode:
            raise CliUndecodableError(
                f"exit {self.returncode} but its {result.__name__} "
                f"answers exit {answer.exit_code()}: {self.stdout.strip()}"
            )
        return answer

    def decoded[Answer](self, result: type[Answer]) -> Answer:
        """Decode a selected-runtime report or raise its declared failure with stderr."""
        if self.returncode:
            try:
                failure = CORE_ERROR.validate_json(self.stdout)
            except ValidationError:
                pass  # a batch's own result also answers a nonzero exit
            else:
                raise CliRefusedError(failure)
        try:
            answer = FieldCodec.decode(result, FieldCodec.parse(self.stdout))
        except (MalformedRecord, ValueError) as malformed:
            raise CliUndecodableError(
                f"exit {self.returncode}, {malformed}; stdout: {self.stdout.strip()}; "
                f"stderr: {self.stderr.strip()}"
            ) from malformed
        return answer


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

    def entry(self) -> tuple[str, ...]:
        return "-m", "agent_comms.cli"

    def source(self) -> str:
        """Ordinary CLI invocations supply no stdin; scripted probes override it."""
        return ""

    def run(self) -> CliOutput:
        completed = subprocess.run(
            (sys.executable, *self.entry(), *self.arguments),
            capture_output=True,
            input=self.source(),
            text=True,
            check=False,
        )
        return CliOutput(completed.returncode, completed.stdout, completed.stderr)
