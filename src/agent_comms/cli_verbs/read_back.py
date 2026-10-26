from __future__ import annotations

from agent_comms.active_route import CommsRoute
from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import BusReadBack
from agent_comms.comms import Comms
from agent_comms.registry_store import RegistryStore
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


@dataclass(frozen=True, kw_only=True)
class ReadBackCliCommand(CliCommand[BusReadBack], declared_name="read-back"):
    help = ("Count the threads this bus's registry holds; the installer reads a bus back "
            "with it before and after its carries")

    def answer(self, root: Path) -> BusReadBack:
        return BusReadBack(len(RegistryStore.at(root).snapshot().threads))

    def apply(self, ctx: Comms) -> BusReadBack:
        return self.answer(ctx.root)

    def execute(self, route: CommsRoute, output: TextIO) -> int:
        """Before its carries, the installer reads only the existing registry."""
        return self.answer(route.root).write(output)
