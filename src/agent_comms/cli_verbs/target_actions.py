from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_support.target_arguments import TargetArguments
from agent_comms.cli_verbs.cli_command import CliCommand, TargetActionCommand
from agent_comms.cli_verbs.cli_commands import TargetCatalogResult
from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import CommandTarget


@dataclass(frozen=True, kw_only=True)
class TargetActionsCliCommand(TargetArguments, declared_name="target-actions"):
    help = "List applicable operations and their declared parameters"

    def apply(self, ctx: Comms) -> TargetCatalogResult:
        return TargetCatalogResult(TargetActionCommand.target_catalog(
            ctx, CliCommand.selected_targets(CommandTarget.arguments(self.target, self.project)),
            self.channel, project=self.project))
