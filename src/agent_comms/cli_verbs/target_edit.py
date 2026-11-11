from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_verbs.cli_command import TargetActionCommand, TargetEdit
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.cli_verbs.target_action import TargetActionCliCommand
from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import CommandTarget


@dataclass(frozen=True, kw_only=True)
class TargetEditCliCommand(TargetActionCliCommand, declared_name='target-edit'):
    help = 'Execute a declared operation using its original CLI field parsers'

    def apply(self, ctx: Comms) -> CliResult:
        declaration: type[TargetActionCommand] = TargetActionCommand.member_named(self.operation)
        return TargetEdit(declaration,
                          declaration.selected_targets(CommandTarget.arguments(self.target, self.project)), self.arguments,
                          self.confirmed, self.channel, project=self.project).apply(ctx)
