from __future__ import annotations

from agent_comms.active_route import CommsRoute
from agent_comms.active_route import RouteFile
from agent_comms.active_route import active_route_path
from agent_comms.cli_command import CliCommand
from agent_comms.nominal.field_codec import WireExclude
from agent_comms.installer.interpreter_operation import ProcessLauncherPhase
from agent_comms.installer.launch import InstallRun
from agent_comms.installer.logs import InstallLogs
from agent_comms.installer.request import InstallRequest
from agent_comms.launch_root import LaunchRoot
from dataclasses import dataclass
from typing import Annotated
from typing import Literal
from typing import TextIO
import dataclasses


@dataclass(frozen=True, kw_only=True)
class InstallCliCommand(InstallRequest, CliCommand, declared_name=InstallRequest.kind):
    kind: Annotated[Literal["install"], WireExclude()] = dataclasses.field(
        default=InstallRequest.kind, init=False)
    help = ("Upgrade this machine's bus, checkout and runtime to --to, then set up every coding "
            "harness to use the bus, with the commander skill and tools")
    @classmethod
    def published_launch(cls) -> LaunchRoot:
        """Install runs where no route selects a bus yet: the published bus while its route
        still selects it, otherwise the default root, where install creates one."""
        return LaunchRoot.from_path(RouteFile.read(active_route_path()).launch_root())

    def execute(self, route: CommsRoute, output: TextIO) -> int:
        """The bus may be unreadable by this runtime, or absent, until the target's own
        source has carried or created it, so nothing opens it here."""
        with InstallLogs(route.root).launching():
            run = InstallRun.start(self, route.root, ProcessLauncherPhase())
            return run.launch(output)
