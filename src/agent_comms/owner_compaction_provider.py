"""Typed owner summary outcomes and shared selected-native usage validation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .compaction_journal import CompactionOperation
    from .owner_compaction_commit import CompactionSource, OwnerCompactionCommit
    from .selected_summary_admission import SelectedSummaryAdmission
    from .threads import Thread


def valid_native_usage(value: Any) -> bool:
    if (
        type(value) is not dict
        or not {"input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost"} <= value.keys()
        or set(value)
        - {
            "input",
            "output",
            "cacheRead",
            "cacheWrite",
            "totalTokens",
            "cost",
            "reasoning",
            "cacheWrite1h",
        }
    ):
        return False
    counters = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
    if any(type(value[key]) is not int or not 0 <= value[key] <= 2**53 - 1 for key in counters):
        return False
    if any(
        key in value and (type(value[key]) is not int or not 0 <= value[key] <= 2**53 - 1)
        for key in ("reasoning", "cacheWrite1h")
    ):
        return False
    cost = value["cost"]
    if type(cost) is not dict or set(cost) != {
        "input",
        "output",
        "cacheRead",
        "cacheWrite",
        "total",
    }:
        return False
    return all(
        type(amount) in (int, float) and isfinite(amount) and 0 <= amount <= 2**53 - 1
        for amount in cost.values()
    )


class OwnerSummaryOutcome(ABC):
    """The selected outcome owns whether a native write is needed."""

    @abstractmethod
    async def commit_with(
        self, writer: Callable[[NativeSummary], Awaitable[CompactionOperation]]
    ) -> CompactionOperation | None:
        """Write a summary or preserve the unchanged source on a clean decline."""

    def admit_original(
        self,
        bridge: OwnerCompactionCommit,
        owner: Thread,
        owner_generation: int,
        operation: CompactionOperation | None,
        source: CompactionSource,
    ) -> SelectedSummaryAdmission | None:
        return None


@dataclass(frozen=True)
class NativeSummary(OwnerSummaryOutcome):
    text: str
    details: dict[str, list[str]] | None
    usage: dict[str, Any] | None

    async def commit_with(
        self, writer: Callable[[NativeSummary], Awaitable[CompactionOperation]]
    ) -> CompactionOperation:
        return await writer(self)

    def commit_options(self) -> dict[str, Any]:
        """Additional owner-commit binding supplied by a selected summary."""
        return {}
