from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.field_codec import PathText
from agent_comms.installer.interpreter_operation import RuntimeProbe


@dataclass(frozen=True)
class RunningOwnersProbe(RuntimeProbe[list[str]]):
    root: Annotated[Path, PathText]

    def run(self) -> list[str]:
        from agent_comms.comms import wire
        from agent_comms.owner_restart import OwnerRestartRequest

        snapshot = wire(self.root).registry.snapshot()
        return [thread.name for thread in OwnerRestartRequest().scope.threads(snapshot)]
