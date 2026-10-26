"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.channel_targets import CommandTarget
from agent_comms.cli_command import CliCommand
from agent_comms.cli_command import TargetEdit
from agent_comms.cli_result import CliResult
from agent_comms.cli_verbs.target_action import TargetActionCliCommand
from agent_comms.comms import Comms
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class TargetEditCliCommand(TargetActionCliCommand, declared_name='target-edit'):
    help = 'Execute a declared operation using its original CLI field parsers'

    def apply(self, ctx: Comms) -> CliResult:
        declaration = CliCommand.member_named(self.operation)
        return TargetEdit(declaration,
                          declaration.selected_targets(CommandTarget.arguments(self.target, self.project)), self.arguments,
                          self.confirmed, self.channel, self.project).apply(ctx)
