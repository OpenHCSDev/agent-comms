"""A CLI command's result owns its exit code, reconnected targets and JSON."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TextIO

from .pi_vocabulary import ServiceTier
from .tool_result import DeclaredReply


class ResultShower(ABC):
    """A front end that displays successful and failed command outcomes."""

    @abstractmethod
    def show_done(self, text: str, title: str) -> None: ...

    @abstractmethod
    def show_failure(self, text: str, title: str) -> None: ...


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
class ToolCatalogResult(CliResult):
    tools: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class ThreadToolCatalogResult(ToolCatalogResult):
    service_tier: ServiceTier
    launch_arguments: tuple[str, ...]
