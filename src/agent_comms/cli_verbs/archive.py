"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_command import MultipleTargets
from agent_comms.cli_commands import ThreadArchivedResult
from agent_comms.cli_support.named_thread import NamedThreadCliCommand
from agent_comms.comms import Comms
from agent_comms.thread_execution import ArchiveOwnerControl
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class ArchiveCliCommand(MultipleTargets, ArchiveOwnerControl, NamedThreadCliCommand,
                        CliCommand[ThreadArchivedResult]):
    help = "Archive a stopped thread"

    def apply(self, ctx: Comms) -> ThreadArchivedResult:
        ctx.threads.archive(self.name)
        return ThreadArchivedResult(self.name)
