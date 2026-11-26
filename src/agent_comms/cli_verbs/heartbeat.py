from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_verbs.cli_verb import CliBus, CliCommand
from agent_comms.cli_verbs.cli_commands import HeartbeatResult, NameArgument
from agent_comms.cli_verbs.cli_result import CliResult


@dataclass(frozen=True, kw_only=True)
class HeartbeatCliCommand(NameArgument, CliCommand):
    help = "Mark thread running"

    def apply(self, ctx: CliBus) -> CliResult:
        ctx.threads.heartbeat(self.name)
        return HeartbeatResult(self.name)
