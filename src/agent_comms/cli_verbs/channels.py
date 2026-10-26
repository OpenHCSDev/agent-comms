from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import ChannelsResult
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class ChannelsCliCommand(CliCommand):
    help = "Derived channel list"

    def apply(self, ctx: Comms) -> CliResult:
        return ChannelsResult(tuple(ctx.channels.channels()))
