from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import TeammatesResult
from agent_comms.team.config import TeamChoice
from agent_comms.team.config import KeptTeam
from agent_comms.team.config import KeptRepos
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class TeamCliCommand(CliCommand, declared_name="commander"):
    """Select the board/repository storage lead, not a rank over collaborators."""

    help = "Name the storage lead and list independent collaborators"
    lead: Annotated[
        TeamChoice,
        Argument(flags=("--lead",), options={"help": "Record this thread as the lead teammate"}),
    ] = KeptTeam()

    def apply(self, ctx: Comms) -> TeammatesResult:
        snapshot = ctx.registry.snapshot()
        project = self.lead.configured(snapshot.commander_project, KeptRepos(), snapshot)
        if project != snapshot.commander_project:
            ctx.registry.set_team_project(project)
        snapshot = ctx.registry.snapshot()
        project = snapshot.commander_project
        return TeammatesResult(
            tuple(thread.name for thread in project.leads(snapshot)),
            tuple(thread.name for thread in project.commanders(snapshot)))
