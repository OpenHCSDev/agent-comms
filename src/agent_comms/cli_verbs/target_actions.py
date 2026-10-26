"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.channel_targets import CommandTarget
from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import TargetCatalogResult
from agent_comms.cli_support.target_arguments import TargetArguments
from agent_comms.comms import Comms
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class TargetActionsCliCommand(TargetArguments, declared_name="target-actions"):
    help = "List applicable operations and their declared parameters"

    def apply(self, ctx: Comms) -> TargetCatalogResult:
        return TargetCatalogResult(CliCommand.target_catalog(
            ctx, CliCommand.selected_targets(CommandTarget.arguments(self.target, self.project)),
            self.channel, project=self.project))
