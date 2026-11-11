from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import NameArgument, ThreadArchivedResult
from agent_comms.comms import Comms
from agent_comms.transport.tool_caller import SubagentCaller


@dataclass(frozen=True, kw_only=True)
class RetireSlotCliCommand(NameArgument, CliCommand[ThreadArchivedResult],
                           declared_name="retire-slot"):
    help = "Archive a subagent's slot when its occupant stops; the next occupant redeclares onto it"

    def apply(self, ctx: Comms) -> ThreadArchivedResult:
        SubagentCaller(self.name).end(ctx)
        return ThreadArchivedResult(self.name)
