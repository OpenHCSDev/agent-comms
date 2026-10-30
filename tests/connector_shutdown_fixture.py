"""Controlled tool declarations served by the real connector in shutdown proofs."""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.comms import Comms
from agent_comms.connector import ServeVerb
from agent_comms.connector_store import ConnectorStore
from agent_comms.field_codec import PathText
from agent_comms.launch_root import LaunchRoot
from agent_comms.tool_caller import ToolCaller
from agent_comms.tool_request import ReadTool, ToolRequest
from agent_comms.tool_result import ToolResult


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
class ShutdownReadTool(ReadTool, HeldCall, ToolRequest):
    label = "Held shutdown read"
    description = "A controlled read with no bus mutation."


@dataclass(frozen=True, kw_only=True)
class ShutdownWriteTool(HeldCall, ToolRequest):
    label = "Held shutdown write"
    description = "A controlled write that publishes only after release."

    def completed(self, comms: Comms, caller: ToolCaller) -> ToolResult:
        comms.messaging.send(caller.own_thread(), "worker", "shutdown write committed")
        return ShutdownReply()


if __name__ == "__main__":
    root, auth, port = sys.argv[1:]
    with LaunchRoot.from_path(Path(root)).selected():
        raise SystemExit(ServeVerb(root=Path(root), port=int(port)).apply(ConnectorStore.at(Path(auth))))
