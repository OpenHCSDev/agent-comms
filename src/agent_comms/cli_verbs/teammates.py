from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import TeammatesResult
from agent_comms.comms import Comms


@dataclass(frozen=True, kw_only=True)
class TeammatesCliCommand(CliCommand, declared_name="teammates"):
    """The independent colleagues on this bus's team; nobody among them is its lead."""

    help = "List the independent collaborators on this bus's team"

    def apply(self, ctx: Comms) -> TeammatesResult:
        snapshot = ctx.registry.snapshot()
        return TeammatesResult(
            tuple(thread.name for thread in snapshot.team_project.colleagues(snapshot))
        )
