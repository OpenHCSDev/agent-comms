from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import CommandersResult
from agent_comms.commander.config import CommanderChoice
from agent_comms.commander.config import KeptCommander
from agent_comms.commander.config import KeptRepos
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class CommanderCliCommand(CliCommand):
    """Select the board/repository storage lead, not a rank over collaborators."""

    help = "Name the storage lead and list independent collaborators"
    lead: Annotated[
        CommanderChoice,
        Argument(flags=("--lead",), options={"help": "Record this thread as the lead commander"}),
    ] = KeptCommander()

    def apply(self, ctx: Comms) -> CommandersResult:
        snapshot = ctx.registry.snapshot()
        project = self.lead.configured(snapshot.commander_project, KeptRepos(), snapshot)
        if project != snapshot.commander_project:
            ctx.registry.set_commander_project(project)
        snapshot = ctx.registry.snapshot()
        project = snapshot.commander_project
        return CommandersResult(
            tuple(thread.name for thread in project.leads(snapshot)),
            tuple(thread.name for thread in project.commanders(snapshot)))
