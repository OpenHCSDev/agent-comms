from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import NameArgument
from agent_comms.cli_commands import ThreadArchivedResult
from agent_comms.comms import Comms
from agent_comms.tool_caller import SubagentCaller
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class RetireSlotCliCommand(NameArgument, CliCommand[ThreadArchivedResult],
                           declared_name="retire-slot"):
    help = "Archive a subagent's slot when its occupant stops; the next occupant redeclares onto it"

    def apply(self, ctx: Comms) -> ThreadArchivedResult:
        SubagentCaller(self.name).retire(ctx)
        return ThreadArchivedResult(self.name)
