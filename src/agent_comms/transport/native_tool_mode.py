"""Owner-bound native tool policy ABC shared by selected and coding turns."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class OwnerToolConnection:
    """The private tool endpoint for one input, never a process setting."""

    path: str
    token: str = field(repr=False)


class ToolTransportEndpoint(ABC):
    """A prepared tool endpoint awaiting its child process PID."""

    @abstractmethod
    async def bind_process(self, pid: int): ...


class NativeToolMode(ABC):
    """Owner-bound native tool policy shared by selected and normal coding turns."""

    @abstractmethod
    def launch_arguments(self, package: Path) -> tuple[str, ...]:
        """Return the native tool selection and reviewed extension."""

    @abstractmethod
    def allowed_tools(self) -> tuple[str, ...]:
        """The tools this mode's prompt allows the model, out of those its child loaded. It
        travels on the prompt, so modes that differ only here share one child."""

    @abstractmethod
    async def open_transport(self, turn, ownership) -> ToolTransportEndpoint:
        """Open the transport implementing this declared permission."""

    def or_else(self, factory: Callable[[], NativeToolMode]) -> NativeToolMode:
        return self
