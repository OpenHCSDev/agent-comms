from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, TextIO

from agent_comms.cli_verbs.cli_verb import BusOpener, CliBus, CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.installer.active_route import CommsRoute
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.messaging.publisher import OwnerPublisher
from agent_comms.nominal.field_codec import Argument, WireForm
from agent_comms.planning.goal_snapshot_reader import GoalSnapshotReader
from agent_comms.presentation import ThreadRecord
from agent_comms.registry.route_client import ActivityClient


@dataclass(frozen=True)
class ThreadsResult(CliResult):
    threads: tuple[Annotated[ThreadRecord, WireForm], ...]


@dataclass(frozen=True, kw_only=True)
class ThreadsCliCommand(CliCommand):
    help = "List threads"
    active_only: Annotated[bool, Argument(flags=("--active-only",))] = (
        False
    )

    def apply(self, ctx: CliBus) -> ThreadsResult:
        return ThreadsResult(ctx.views.list_threads(active_only=self.active_only))

    def execute(self, route: CommsRoute, output: TextIO, open_bus: BusOpener) -> int:
        with ActivityClient.client(route) as client:
            bus = MessageBus(client.root, client.registry, OwnerPublisher)
            goals = GoalSnapshotReader(client.root, client.agents)
            return ThreadsResult(ThreadRecord.list_threads(
                client.registry.snapshot(), bus, client.agents, goals, active_only=self.active_only,
            )).write(output)
