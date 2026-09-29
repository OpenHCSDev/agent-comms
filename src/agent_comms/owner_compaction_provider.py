"""Typed owner summary outcomes and shared selected-native usage validation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .agent_events import CompactionEnd
from .pi_summary_payloads import SummaryFiles, SummaryUsage

if TYPE_CHECKING:
    from .compaction_records import CompactionOperation
    from .compaction_source import CompactionSource
    from .owner_compaction_commit import OwnerCompactionCommit
    from .selected_summary_admission import SelectedSummaryAdmission
    from .threads import Thread


class OwnerSummaryOutcome(ABC):
    """The selected outcome owns whether a native write is needed."""

    @abstractmethod
    async def commit_with(
        self, writer: Callable[[NativeSummary], Awaitable[CompactionOperation]]
    ) -> CompactionOperation | None:
        """Write a summary or preserve the unchanged source on a clean decline."""

    @property
    @abstractmethod
    def completion_event(self) -> CompactionEnd:
        """Report only a completed, authoritative outcome."""

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
    details: SummaryFiles | None
    usage: SummaryUsage | None

    async def commit_with(
        self, writer: Callable[[NativeSummary], Awaitable[CompactionOperation]]
    ) -> CompactionOperation:
        operation = await writer(self)
        operation.state.require_committed(operation.commit_id)
        return operation

    @property
    def completion_event(self) -> CompactionEnd:
        return CompactionEnd(reason="adaptive", summary=self.text)

    def commit_options(self) -> dict[str, Any]:
        """Additional owner-commit binding supplied by a selected summary."""
        return {}
