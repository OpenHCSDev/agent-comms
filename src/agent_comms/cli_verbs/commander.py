from __future__ import annotations

from agent_comms.channel_targets import CommandersTag
from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import CommandersResult
from agent_comms.cli_commands import _tags
from agent_comms.commander.config import CommanderChoice
from agent_comms.commander.config import KeptCommander
from agent_comms.commander.config import KeptRepos
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import Argument
from agent_comms.field_codec import Normalize
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class CommanderCliCommand(CliCommand):
    """Assign this bus's commanders at runtime, through the same owner install's --commander
    uses: the lead is named on the registry's commander project, keeping its repositories,
    and a non-lead commander leaves by dropping its #commanders tag."""

    help = "Name the lead commander or remove a commander at runtime"
    lead: Annotated[
        CommanderChoice,
        Argument(flags=("--lead",), options={"help": "Record this thread as the lead commander"}),
    ] = KeptCommander()
    remove: Annotated[
        frozenset[str],
        Normalize(_tags),
        Argument(flags=("--remove",), options={"help": "Remove these threads as commanders, separated by commas"}),
    ] = frozenset()

    def apply(self, ctx: Comms) -> CommandersResult:
        snapshot = ctx.registry.snapshot()
        project = self.lead.configured(snapshot.commander_project, KeptRepos(), snapshot)
        if project != snapshot.commander_project:
            ctx.registry.set_commander_project(project)
        for name in self.remove:
            if project.by_role(ctx.registry.require(name).incarnation, commander=True, commanded=False):
                raise RelationViolationError(f"{name} is the lead commander; name another lead with --lead")
            ctx.channels.update_tags(name, remove=frozenset({CommandersTag.declared_name}))
        snapshot = ctx.registry.snapshot()
        project = snapshot.commander_project
        return CommandersResult(
            tuple(thread.name for thread in project.leads(snapshot)),
            tuple(thread.name for thread in project.commanders(snapshot)))
