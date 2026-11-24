"""Retired UI views whose bus source no longer exists."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.transport.tool_result import ToolResult


@dataclass(frozen=True)
class RetiredViews(ToolResult):
    """Views whose source no longer exists, as Core decided from one registry read."""

    threads: frozenset[ThreadIncarnation]
    channels: frozenset[str]
