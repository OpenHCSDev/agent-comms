"""Thread-specific native launch settings and combined tool-catalog results."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_verbs.cli_result import CliResult, ToolCatalogResult
from agent_comms.execution.pi_vocabulary import ServiceTier


@dataclass(frozen=True)
class ThreadLaunchSettings(CliResult):
    """What one thread's native launch needs beyond the tools every thread shares."""

    service_tier: ServiceTier
    launch_arguments: tuple[str, ...]


@dataclass(frozen=True)
class ThreadToolCatalogResult(ThreadLaunchSettings, ToolCatalogResult):
    """The tools and one thread's launch settings together, for a caller that asks once."""
