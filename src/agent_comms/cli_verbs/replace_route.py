from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import RouteReplacedResult
from agent_comms.comms import Comms
from agent_comms.installer.active_route import (
    ActiveRoute,
    CommsRoute,
    active_route_path,
    route_new_bus,
)
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.recorded_bus import RecordedBusIdentity


@dataclass(frozen=True, kw_only=True)
class ReplaceRouteCliCommand(CliCommand[RouteReplacedResult],
                             declared_name=ActiveRoute.repair_command):
    help = ("Route to --root's bus unless the active route file selects a bus; a file that "
            "selects none (unreadable, an old version, or naming a root gone or replaced) "
            "is kept beside the new one")

    def answer(self, root: Path) -> RouteReplacedResult:
        route = ActiveRoute(root, RecordedBusIdentity.capture(Bus.path_in(root)))
        return RouteReplacedResult(str(root), route_new_bus(route, active_route_path()))

    def apply(self, ctx: Comms) -> RouteReplacedResult:
        return self.answer(ctx.root)

    def execute(self, route: CommsRoute, output: TextIO) -> int:
        """Every client borrows the published route, and a route that selects no bus refuses
        each borrower, so the repair of that route borrows none."""
        return self.answer(route.root).write(output)
