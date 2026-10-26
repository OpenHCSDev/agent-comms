"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_bridges.history import DefaultAddress
from agent_comms.cli_bridges.history import HarnessAddress
from agent_comms.cli_bridges.history import OnlineHistory
from agent_comms.cli_command import CliCommand
from agent_comms.field_codec import Argument
from agent_comms.field_codec import Editor
from agent_comms.harness_install import Harness
from agent_comms.target_fields import ChoiceTargetField
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class RunningSessionCliCommand(CliCommand):
    """A session a running harness holds, asked for by the harness's own id."""

    harness: Annotated[
        type[Harness],
        Editor(ChoiceTargetField),
        Argument(flags=("--harness",)),
    ]
    thread: Annotated[
        str,
        Argument(
            flags=("--thread",), options={"help": "The harness's own session or thread id"}
        ),
    ]
    address: Annotated[
        HarnessAddress,
        Argument(
            flags=("--address",),
            options={"help": "Codex app-server socket, or OpenCode server URL"},
        ),
    ] = DefaultAddress()

    def history(self, directory: Path) -> OnlineHistory:
        return OnlineHistory.of(self.harness, self.thread, self.address, directory)
