from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_verbs.cli_verb import CliBus, CliCommand
from agent_comms.cli_verbs.cli_commands import NameArgument, OwnerReleasedResult
from agent_comms.cli_verbs.cli_result import CliResult


@dataclass(frozen=True, kw_only=True)
class ReleaseCliCommand(NameArgument, CliCommand):
    help = "Voluntarily mark the calling thread stopped"

    def apply(self, ctx: CliBus) -> CliResult:
        ctx.owners.release(self.name)
        return OwnerReleasedResult(self.name)
