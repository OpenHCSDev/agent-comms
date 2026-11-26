from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from agent_comms.cli_verbs.cli_verb import BusOpener, CliBus, CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.installer.active_route import CommsRoute
from agent_comms.registry.registry_store import RegistryStore


@dataclass(frozen=True)
class BusReadBack(CliResult):
    """What a bus's registry holds that a carry must keep: its threads."""

    threads: int

    def lines(self) -> tuple[str, ...]:
        """A read-back proves the carried bus reads; only an answered message proves an
        agent works on it, and an install answers none."""
        return (f"Read back: {self.threads} threads",
                "No live agent has answered a message on this install yet")


@dataclass(frozen=True, kw_only=True)
class ReadBackCliCommand(CliCommand[BusReadBack], declared_name="read-back"):
    help = ("Count the threads this bus's registry holds; the installer reads a bus back "
            "with it before and after its carries")

    def answer(self, root: Path) -> BusReadBack:
        return BusReadBack(len(RegistryStore.at(root).snapshot().threads))

    def apply(self, ctx: CliBus) -> BusReadBack:
        return self.answer(ctx.root)

    def execute(self, route: CommsRoute, output: TextIO, open_bus: BusOpener) -> int:
        """Before its carries, the installer reads only the existing registry."""
        return self.answer(route.root).write(output)
