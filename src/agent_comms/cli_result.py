"""A CLI command's result owns its exit code, reconnected targets and JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TextIO

from .pi_vocabulary import ServiceTier
from .tool_result import DeclaredReply


class CliResult(DeclaredReply):
    """Every result a command returns; a result kind overrides what it decides."""

    __slots__ = ()

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
