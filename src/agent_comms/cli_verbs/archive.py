from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_support.named_thread import NamedThreadCliCommand
from agent_comms.cli_verbs.cli_command import MultipleTargets, TargetActionCommand
from agent_comms.cli_verbs.cli_commands import ThreadArchivedResult
from agent_comms.comms import Comms
from agent_comms.registry.threads import ArchiveOwnerControl


@dataclass(frozen=True, kw_only=True)
class ArchiveCliCommand(MultipleTargets, ArchiveOwnerControl, NamedThreadCliCommand,
                        TargetActionCommand[ThreadArchivedResult]):
    help = "Archive a stopped thread"

    def apply(self, ctx: Comms) -> ThreadArchivedResult:
        ctx.threads.archive(self.name)
        return ThreadArchivedResult(self.name)
