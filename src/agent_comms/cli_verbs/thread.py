from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import NameArgument
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument, FieldCodec, Normalize
from agent_comms.presentation import CountedRead, InboxRead, UncountedRead


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
