from __future__ import annotations

from dataclasses import dataclass
from typing import TextIO

from agent_comms.cli_verbs.cli_command import BusOpener, CliBus, CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.root_runtime import RootRuntime
from agent_comms.installer.active_route import CommsRoute
from agent_comms.messaging.bus import Bus
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
        RootRuntime.require_attachable(route.root, LaunchRuntime.running())
        return ChannelsResult(
            tuple(ChannelCatalog.at(route.root).read().views(Bus(route.root).activity_clocks()))
        ).write(output)
