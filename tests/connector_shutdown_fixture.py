"""Controlled tool declarations served by the real connector in shutdown proofs."""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.comms import Comms
from agent_comms.transport.connector import ServeVerb
from agent_comms.execution.launch_root import LaunchRoot, started
from agent_comms.nominal.field_codec import PathText
from agent_comms.transport.connector_store import ConnectorStore
from agent_comms.transport.tool_caller import ToolCaller
from agent_comms.transport.tool_request import ReadTool, ServedRequest
from agent_comms.transport.tool_result import ToolResult


@dataclass(frozen=True)
class ShutdownReply(ToolResult):
    completed: bool = True


@dataclass(frozen=True, kw_only=True)
class HeldCall:
    directory: Annotated[Path, PathText]

    def wait(self) -> None:
        (self.directory / "entered").touch()
        deadline = time.monotonic() + 20
        while not (self.directory / "release").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Shutdown proof did not release its held tool")
            time.sleep(.01)

    def apply(self, comms: Comms, caller: ToolCaller) -> ToolResult:
        self.wait()
        result = self.completed(comms, caller)
        (self.directory / "finished").touch()
        return result

    def completed(self, comms: Comms, caller: ToolCaller) -> ToolResult:
        return ShutdownReply()


@dataclass(frozen=True, kw_only=True)
class ShutdownReadTool(ReadTool, HeldCall, ServedRequest):
    label = "Held shutdown read"
    description = "A controlled read with no bus mutation."


@dataclass(frozen=True, kw_only=True)
class ShutdownWriteTool(HeldCall, ServedRequest):
    label = "Held shutdown write"
    description = "A controlled write that publishes only after release."

    def completed(self, comms: Comms, caller: ToolCaller) -> ToolResult:
        comms.messaging.send(caller.own_thread(), "worker", "shutdown write committed")
        return ShutdownReply()


if __name__ == "__main__":
    root, auth, port = sys.argv[1:]
    started()  # This file is the process entry, as connector.main is for the real one.
    with LaunchRoot.from_path(Path(root)).selected():
        raise SystemExit(ServeVerb(root=Path(root), port=int(port)).apply(ConnectorStore.at(Path(auth))))
