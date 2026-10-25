from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Annotated

from agent_comms.comms import wire
from agent_comms.field_codec import PathText
from agent_comms.installer.interpreter_operation import RuntimeProbe
from agent_comms.installer.probes.commands import CliCommandProbe
from agent_comms.owner_restart import OwnerRestartRequest


class OwnerActionsProbe(CliCommandProbe):
    """Execute and decode the batch in one isolated, selected-runtime process.

    The entry source ships with the selected runtime's own installer.
    """

    def entry(self) -> tuple[str, ...]:
        return ("-",)

    def source(self) -> str:
        return files(__package__).joinpath("owner_actions.py").read_text()


@dataclass(frozen=True)
class RunningOwnersProbe(RuntimeProbe[list[str]]):
    root: Annotated[Path, PathText]

    def run(self) -> list[str]:
        snapshot = wire(self.root).registry.snapshot()
        return [thread.name for thread in OwnerRestartRequest().scope.threads(snapshot)]
