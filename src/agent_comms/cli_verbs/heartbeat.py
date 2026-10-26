"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import HeartbeatResult
from agent_comms.cli_commands import NameArgument
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class HeartbeatCliCommand(NameArgument, CliCommand):
    help = "Mark thread running"

    def apply(self, ctx: Comms) -> CliResult:
        ctx.threads.heartbeat(self.name)
        return HeartbeatResult(self.name)
