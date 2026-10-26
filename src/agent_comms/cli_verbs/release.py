from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import NameArgument
from agent_comms.cli_commands import OwnerReleasedResult
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class ReleaseCliCommand(NameArgument, CliCommand):
    help = "Voluntarily mark the calling thread stopped"

    def apply(self, ctx: Comms) -> CliResult:
        ctx.owners.release(self.name)
        return OwnerReleasedResult(self.name)
