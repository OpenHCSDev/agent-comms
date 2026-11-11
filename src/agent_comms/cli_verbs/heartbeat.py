from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import HeartbeatResult, NameArgument
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms


@dataclass(frozen=True, kw_only=True)
class HeartbeatCliCommand(NameArgument, CliCommand):
    help = "Mark thread running"

    def apply(self, ctx: Comms) -> CliResult:
        ctx.threads.heartbeat(self.name)
        return HeartbeatResult(self.name)
