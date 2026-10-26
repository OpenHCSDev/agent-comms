from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import NameArgument
from agent_comms.comms import Comms
from agent_comms.thread_management import ThreadDeleted
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class DeleteCliCommand(NameArgument, CliCommand, declared_name="delete"):
    help = "Permanently delete a stopped or archived thread; its bus history stays"

    def apply(self, ctx: Comms) -> ThreadDeleted:
        return ctx.threads.delete(self.name)
