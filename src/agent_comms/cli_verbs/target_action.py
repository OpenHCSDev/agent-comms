"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.channel_targets import CommandTarget
from agent_comms.cli_command import CliCommand
from agent_comms.cli_result import CliResult
from agent_comms.cli_verbs.target_arguments import TargetArguments
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from dataclasses import dataclass
from pydantic import JsonValue
from typing import Annotated
import dataclasses


@dataclass(frozen=True, kw_only=True)
class TargetActionCliCommand(TargetArguments, declared_name="target-action"):
    help = "Execute a declared operation against its current target"
    operation: Annotated[str, Argument(flags=("--operation",))]
    arguments: Annotated[
        dict[str, JsonValue],
        Argument(
            flags=("--arguments",)
        ),
    ] = dataclasses.field(default_factory=dict)
    confirmed: Annotated[bool, Argument(flags=("--confirmed",))] = (
        False
    )

    def apply(self, ctx: Comms) -> CliResult:
        declaration = CliCommand.member_named(self.operation)
        return declaration.execute_target(
            ctx, declaration.selected_targets(CommandTarget.arguments(self.target, self.project)), self.arguments,
            confirmed=self.confirmed, channel=self.channel, project=self.project)
