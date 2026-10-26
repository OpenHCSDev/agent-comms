"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.activity import ActivityState
from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import ActivitySetResult
from agent_comms.cli_commands import NameArgument
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import Editor
from agent_comms.target_fields import ChoiceTargetField
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class ActivityCliCommand(NameArgument, CliCommand):
    help = "Publish current thread activity"
    state: Annotated[
        ActivityState,
        Editor(ChoiceTargetField),
        Argument(flags=("--state",)),
    ]
    detail: Annotated[str, Argument(flags=("--detail",))] = ""

    def apply(self, ctx: Comms) -> CliResult:
        ctx.agents.set_activity(self.name, self.state, self.detail)
        return ActivitySetResult(self.name, self.state.value)
