from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_verbs.cli_verb import CliBus
from agent_comms.cli_support.target_arguments import TargetArguments
from agent_comms.cli_verbs.cli_command import TargetAction, TargetActionCommand, TargetCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.messaging.channel_targets import CommandTarget


@dataclass(frozen=True)
class TargetCatalogResult(CliResult):
    actions: tuple[TargetAction, ...]

    def to_wire(self) -> object:
        return {'actions': [action.encode() for action in self.actions]}


@dataclass(frozen=True, kw_only=True)
class TargetActionsCliCommand(TargetArguments, declared_name="target-actions"):
    help = "List applicable operations and their declared parameters"

    def apply(self, ctx: CliBus) -> TargetCatalogResult:
        return TargetCatalogResult(TargetActionCommand.target_catalog(
            ctx, TargetCommand.selected_targets(CommandTarget.arguments(self.target, self.project)),
            self.channel, project=self.project))
