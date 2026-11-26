from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_verbs.cli_verb import CliBus, CliCommand
from agent_comms.cli_verbs.cli_commands import NameArgument, ThreadDeleted


@dataclass(frozen=True, kw_only=True)
class DeleteCliCommand(NameArgument, CliCommand, declared_name="delete"):
    help = "Permanently delete a stopped or archived thread; its bus history stays"

    def apply(self, ctx: CliBus) -> ThreadDeleted:
        return ctx.threads.delete(self.name)
