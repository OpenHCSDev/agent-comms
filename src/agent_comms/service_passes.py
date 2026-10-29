"""Original completion times and current failures of Core's service watches."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Annotated, ClassVar, Self

from .locked_store import DocumentChange, DurableFile, LockedStore
from .field_codec import WireTransient, projected
from .tool_result import ToolResult
from .coordinator import Coordination
from .recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from .store_files import DependentStateTier, SingleFileStore
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation


@dataclass(frozen=True)
class FailingPass:
    """A watch failing since its first failed pass, with its newest error."""

    since: float
    error: str


@dataclass(frozen=True)
class ServicePasses:
    failing: dict[str, FailingPass] = field(default_factory=dict)
    commander_completed: dict[RecordedThreadIncarnation, float] = field(default_factory=dict)
    """Last observation completed for each commander, including quiet observations."""

    def heartbeat_completed_at(self, commander: RecordedThreadIncarnation) -> EventTime:
        """The original completion clock, or its declared unrecorded state."""
        if commander in self.commander_completed:
            return RecordedEventTime(self.commander_completed[commander])
        return UnrecordedEventTime()

    def stall(self) -> str:
        """Why passes stopped completing: every recorded failure with its error, or, with none
        recorded, that no watch is running at all."""
        return "; ".join(
            f"{watch} failing since {RecordedEventTime(failure.since).clock()}: {failure.error}"
            for watch, failure in self.failing.items()
        ) or "no failure recorded, so no heartbeat is running"

    def completed_since(self, commander: RecordedThreadIncarnation, since: float) -> bool:
        """A pass for this commander completed at or after ``since``."""
        return self.heartbeat_completed_at(commander).since(since)


@dataclass(frozen=True)
class ServiceReading(ToolResult):
    """Read-time service evidence shared by board, load and independent edit replies."""

    services: Annotated[ServicePasses, WireTransient()] = field(
        default_factory=ServicePasses, compare=False, kw_only=True)
    heartbeat: Annotated[EventTime, WireTransient()] = field(
        default_factory=UnrecordedEventTime, compare=False, kw_only=True)

    def with_services(self, passes: ServicePasses, owner: RecordedThreadIncarnation) -> Self:
        return replace(self, services=passes, heartbeat=passes.heartbeat_completed_at(owner))

    def retained_status(self) -> str:
        if self.heartbeat.since(time.time() - 3 * Coordination.watch_period):
            return 'Retained board rows and load:'
        return 'STALE retained board rows and load; no recent completed pass:'

    @projected(view='wire')
    def service_status(self) -> tuple[str, ...]:
        # Standing-rule rendering remains the determining owner of failure text.
        from .standing_rules import ServicePassFailing
        clock = self.heartbeat.aged_line('Tristan last completed', time.time())
        return (*(failure.line() for failure in ServicePassFailing.from_passes(self.services)),
                *(clock or ('Tristan last completed: unrecorded',)))


@dataclass(frozen=True, slots=True)
class ServicePassStore(SingleFileStore, DurableFile, LockedStore[ServicePasses], DependentStateTier):
    """The connector's log reaches nobody; the commander's standing rules read this file."""

    filename: ClassVar[str] = "service_passes.json"
    record_type = ServicePasses

    def empty(self) -> ServicePasses:
        return ServicePasses()

    def failed(self, watch: str, error: BaseException) -> None:
        def change(passes: ServicePasses) -> DocumentChange[ServicePasses, ServicePasses]:
            since = passes.failing.get(watch, FailingPass(time.time(), "")).since
            failing = FailingPass(since, f"{type(error).__name__}: {error}")
            return DocumentChange.to(replace(passes, failing=passes.failing | {watch: failing}))

        self.update(change)

    def passed(self, watch: str) -> None:
        """A pass that succeeds ends its failure; an unfailed watch writes nothing."""
        if watch in self.read().failing:
            self.update(
                lambda passes: DocumentChange.to(replace(
                    passes,
                    failing={name: failure for name, failure in passes.failing.items() if name != watch},
                ))
            )

    def heartbeat_passed(
        self, commanders: Iterable[ThreadIncarnation], registered: Iterable[ThreadIncarnation]
    ) -> None:
        """Only a retired incarnation loses its completion; a missed live one keeps it."""
        current = frozenset(registered)
        completed = {owner.require_recorded(): time.time() for owner in commanders}

        def change(passes: ServicePasses) -> DocumentChange[ServicePasses, ServicePasses]:
            retained = {owner: at for owner, at in passes.commander_completed.items()
                        if owner in current}
            updated = retained | completed
            return DocumentChange.to(
                passes if updated == passes.commander_completed
                else replace(passes, commander_completed=updated)
            )

        self.update(change)
