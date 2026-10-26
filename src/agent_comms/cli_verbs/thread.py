"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import NameArgument
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import FieldCodec
from agent_comms.field_codec import Normalize
from agent_comms.presentation import CountedRead
from agent_comms.presentation import InboxRead
from agent_comms.presentation import UncountedRead
from dataclasses import dataclass
from typing import Annotated
import dataclasses


@dataclass(frozen=True, kw_only=True)
class ThreadCliCommand(NameArgument, CliCommand):
    help = "Detail for one thread"
    read: Annotated[
        InboxRead,
        Normalize(dict),
        Argument(
            flags=("--no-pending",),
            options={
                "help": "Skip the inbox count for fast metadata reads",
                "action": "store_const",
                "const": FieldCodec.encode(UncountedRead()),
            },
        ),
    ] = dataclasses.field(default_factory=CountedRead)

    def apply(self, ctx: Comms) -> CliResult:
        return ctx.views.thread_detail(self.name, self.read)
