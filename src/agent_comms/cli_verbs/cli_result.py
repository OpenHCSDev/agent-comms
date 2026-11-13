"""A CLI command's result owns its exit code, reconnected targets and JSON."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TextIO, cast

from pydantic import JsonValue

from agent_comms.execution.pi_vocabulary import ServiceTier
from agent_comms.nominal.field_codec import SubclassUnion
from agent_comms.transport.tool_result import DeclaredReply, ToolResult


class ResultShower(ABC):
    """A front end that displays successful and failed command outcomes."""

    @abstractmethod
    def show_done(self, text: str, title: str) -> None: ...

    @abstractmethod
    def show_error(self, error: Exception | str, title: str) -> None: ...


class CliResult(DeclaredReply):
    """Every result a command returns; a result kind overrides what it decides."""

    __slots__ = ()

    def show(self, shower: ResultShower, label: str, title: str) -> None:
        shower.show_done(label, title)

    def reconnect_targets(self) -> tuple[str, ...]:
        return ()

    def open_targets(self) -> tuple[str, ...]:
        return ()

    def exit_code(self) -> int:
        return 0

    def write(self, output: TextIO) -> int:
        """Write this result to the caller's stream; its exit code."""
        json.dump(self.to_wire(), output, indent=2)
        output.write("\n")
        return self.exit_code()


@dataclass(frozen=True)
class RegisteredResult(CliResult):
    registered: str


@dataclass(frozen=True)
class ToolCatalogResult(CliResult):
    tools: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class ThreadLaunchSettings(CliResult):
    """What one thread's native launch needs beyond the tools every thread shares."""

    service_tier: ServiceTier
    launch_arguments: tuple[str, ...]


@dataclass(frozen=True)
class ThreadToolCatalogResult(ThreadLaunchSettings, ToolCatalogResult):
    """The tools and one thread's launch settings together, for a caller that asks once."""


class TargetEditResult(CliResult, ToolResult, SubclassUnion, affix="TargetEditResult"):
    """What a thread or channel action answers. A remote UI receives it from Core and decodes
    it by shape: each member is also the CLI result it already was, with its CLI form."""

    def to_wire(self) -> dict[str, JsonValue]:
        """Where its reply and record parents meet, it answers as its declared reply record."""
        return cast(dict[str, JsonValue], ToolResult.to_wire(self))
