from __future__ import annotations

from abc import abstractmethod
from agent_comms.active_route import CommsRoute
from agent_comms.cli_command import CliCommand
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


@dataclass(frozen=True, kw_only=True)
class StoreReconcileCliCommand[Answer: CliResult](CliCommand[Answer]):
    """Every declared store whose schema differs from this runtime's declarations; each
    member says what it does with one."""

    @abstractmethod
    def answer(self, root: Path) -> Answer:
        """This operation's result from the files declared under the root."""

    def apply(self, ctx: Comms) -> Answer:
        return self.answer(ctx.root)

    def execute(self, route: CommsRoute, output: TextIO) -> int:
        """Reconciliation reads its declared files before opening runtime consumers."""
        return self.answer(route.root).write(output)
