"""Run installed CLI commands over one interpreter's explicit request lifetime."""

from __future__ import annotations

import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO

from agent_comms import cli
from agent_comms.field_codec import FieldCodec


@dataclass(frozen=True)
class CommandReply:
    returncode: int
    stdout: str


@dataclass(frozen=True)
class CommandRequest:
    arguments: tuple[str, ...]

    def run(self) -> CommandReply:
        with StringIO() as output, redirect_stdout(output):
            returncode = cli.main(self.arguments)
            return CommandReply(returncode, output.getvalue())


def main() -> None:
    for line in sys.stdin:
        request = FieldCodec.decode(CommandRequest, FieldCodec.parse(line))
        reply = request.run()
        print(FieldCodec.compact(FieldCodec.encode(reply)), flush=True)


if __name__ == "__main__":
    main()
