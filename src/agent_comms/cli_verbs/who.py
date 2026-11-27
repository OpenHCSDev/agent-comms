from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, TextIO

from agent_comms.cli_verbs.cli_verb import BusOpener, CliBus, CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.installer.active_route import CommsRoute
from agent_comms.nominal.field_codec import WireForm
from agent_comms.planning.goal_snapshot_reader import GoalSnapshotReader
from agent_comms.presentation import ThreadView
from agent_comms.registry.route_client import ActivityClient


@dataclass(frozen=True)
class WhoResult(CliResult):
    who: tuple[Annotated[ThreadView, WireForm], ...]


@dataclass(frozen=True, kw_only=True)
class WhoCliCommand(CliCommand):
    help = "Presence: who is in the chat"

    def apply(self, ctx: CliBus) -> WhoResult:
        return WhoResult(ctx.views.who())

    def execute(self, route: CommsRoute, output: TextIO, open_bus: BusOpener) -> int:
        with ActivityClient.client(route) as client:
            goals = GoalSnapshotReader(client.root, client.agents)
            return WhoResult(ThreadView.who(
                client.registry.snapshot(), client.agents, goals,
            )).write(output)
