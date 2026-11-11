from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import ActivitySetResult, NameArgument
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.cli_verbs.target_fields import ChoiceTargetField
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument, Editor
from agent_comms.registry.activity import ActivityState


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
