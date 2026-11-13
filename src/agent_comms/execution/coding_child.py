"""The thread's coding child, and the prompts that run on it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.execution.native_arguments import NativeArguments
from agent_comms.registry.threads import Thread
from agent_comms.transport.native_tool_transport import (
    NativeToolMode,
    ToolTransport,
    UnboundToolTransport,
)
from agent_comms.transport.native_tools import NativeToolDeclaration


@dataclass(frozen=True, kw_only=True)
class CodingChild:
    """The thread whose coding child runs a prompt."""

    comms: Comms
    thread: Thread


@dataclass(frozen=True)
class CodingChildMode(NativeToolMode):
    """A prompt on the thread's coding child. The child loads every declared tool once and
    each prompt allows what its member names, so a tool-less ask and the coding answer after
    it share the child. Launching one child per member cost two Pi starts per channel
    message, about 2 CPU-seconds each."""

    owner: CodingChild

    def launch_arguments(self, package: Path) -> tuple[str, ...]:
        arguments = ("--tools", ",".join(NativeToolDeclaration.names()),
                     *NativeToolDeclaration.extension_arguments(package))
        return self.owner.comms.threads.native_launch_arguments(
            self.owner.thread, NativeArguments.parse(arguments), package,
        )


class ToollessMode(CodingChildMode):
    """The prompt allows the model no tools; its endpoint still opens, and refuses."""

    def allowed_tools(self) -> tuple[str, ...]:
        return ()

    async def open_transport(self, turn, ownership) -> UnboundToolTransport:
        return await ToolTransport.open(turn, ownership)

    def or_else(self, factory: Callable[[], NativeToolMode]) -> NativeToolMode:
        return factory()
