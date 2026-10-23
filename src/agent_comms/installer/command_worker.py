"""Run declared installer operations in one interpreter, one request per line."""

from __future__ import annotations

import sys
import traceback
from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO

from agent_comms import cli
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.interpreter_operation import (
    CompletedProbeReply, FailedProbeReply, RuntimeProbe,
)


@dataclass(frozen=True)
class CliOutput:
    returncode: int
    stdout: str


@dataclass(frozen=True)
class CliCommandProbe(RuntimeProbe[CliOutput]):
    arguments: tuple[str, ...]

    def run(self) -> CliOutput:
        with StringIO() as output, redirect_stdout(output):
            returncode = cli.main(self.arguments)
            return CliOutput(returncode, output.getvalue())


def main() -> None:
    for line in sys.stdin:
        try:
            with redirect_stdout(sys.stderr):
                probe = FieldCodec.decode(RuntimeProbe, FieldCodec.parse(line))
                reply = CompletedProbeReply(probe.encode_answer(probe.run()))
        except Exception:
            reply = FailedProbeReply(traceback.format_exc())
        print(FieldCodec.compact(FieldCodec.encode(reply)), flush=True)


if __name__ == "__main__":
    main()
