from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO

from pydantic import TypeAdapter, ValidationError

from agent_comms.cli import CliInvocation
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
class CliCommandProbe(CliInvocation, RuntimeProbe[CliOutput]):
    """One CLI invocation in the selected runtime's existing interpreter worker.

    The worker key owns cwd and environment; CliInvocation scopes each call's root.
    """

    arguments: tuple[str, ...]

    def install_phase(self) -> CliProbePhase:
        return CliProbePhase(probe=type(self), arguments=self.arguments)

    def run(self) -> CliOutput:
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            returncode = self.invoke(self.arguments, output)
        return CliOutput(returncode, output.getvalue(), errors.getvalue())
