from __future__ import annotations

from dataclasses import dataclass
from typing import TextIO

from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.cli_verbs.cli_verb import BusOpener, CliBus, CliCommand
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.root_runtime_read import RootRuntimeData
from agent_comms.installer.active_route import CommsRoute
from agent_comms.messaging.bus_activity_index import BusActivityStore
from agent_comms.registry.catalog_store import ChannelCatalog


@dataclass(frozen=True)
class ChannelsResult(CliResult):
    channels: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ChannelsCliCommand(CliCommand):
    help = "Derived channel list"

    def apply(self, ctx: CliBus) -> CliResult:
        return ChannelsResult(tuple(ctx.channels.channels()))

    def execute(self, route: CommsRoute, output: TextIO, open_bus: BusOpener) -> int:
        def read_recorded() -> int:
            route.observe_root()
            return ChannelsResult(
                tuple(
                    ChannelCatalog.at(route.root).read().views(
                        BusActivityStore.at(route.root).activity_clocks()
                    )
                )
            ).write(output)

        def adopt_and_read() -> int:
            with open_bus(route) as comms:
                return self.run(comms, output)

        return RootRuntimeData.recorded_attachment(
            route.root, LaunchRuntime.running(),
        ).run_recorded(read_recorded, adopt_and_read)
