from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_verbs.cli_verb import CliBus
from agent_comms.cli_support.named_thread import NamedThreadCliCommand
from agent_comms.cli_verbs.cli_command import MultipleTargets, TargetActionCommand
from agent_comms.cli_verbs.cli_commands import ThreadArchivedResult
from agent_comms.registry.threads import ArchiveOwnerControl


@dataclass(frozen=True, kw_only=True)
class ArchiveCliCommand(MultipleTargets, ArchiveOwnerControl, NamedThreadCliCommand,
                        TargetActionCommand[ThreadArchivedResult]):
    help = "Archive a stopped thread"
    menu_label = "Archive"

    def apply(self, ctx: CliBus) -> ThreadArchivedResult:
        ctx.threads.archive(self.name)
        return ThreadArchivedResult(self.name)
