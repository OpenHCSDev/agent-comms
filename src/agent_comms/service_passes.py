"""Original completion times and current failures of Core's service watches."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Annotated, ClassVar, Self

from .coordinator import Coordination
from .locked_store import DocumentChange, DurableFile, LockedStore
from agent_comms_hooks import ToolRefusedError
from .field_codec import WireExclude, WireTransient, projected
from .tool_result import ToolResult
from .recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from .store_files import DependentStateTier, SingleFileStore
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .watched import Watched


class CompletedWatch:
    """A service watch whose completion the board judges; each member declares how long its
    longest pass takes."""

    pass_duration: ClassVar[float]

    @classmethod
    def completion_ceiling(cls) -> float:
        """How long a new runtime may take to record its first completion: one cadence (a pass,
        then the service loop's sleep) plus one pass of slack."""
        return 2 * cls.pass_duration + Coordination.watch_period


class CommanderPass(CompletedWatch):
    """The commander's board pass: its service runs it, and its readers judge freshness by it."""

    pass_duration: ClassVar[float] = 270
    """The longest pass on the live bus: first passes after the 2026-10-30 installs took
    190-264 s (c6d81834 restarted 04:32:49, completed 04:36:00)."""


@dataclass(frozen=True)
class FailingPass:
    """A watch failing since its first failed pass, with its newest error and the time of its
    newest failed pass."""

    since: float
    error: str
    failed_at: float

    def failure_line(self, watch: str) -> str:
        return (f'FIX service pass {watch} failing since {RecordedEventTime(self.since).clock()}'
                f' ({max(0, int(time.time() - self.since))}s elapsed):'
                f' {self.error}. is anyone fixing it, or letting it pile up?'
                ' -> fix the owner of the failing read this wake')


@dataclass(frozen=True)
class ServicePasses:
    failing: dict[str, FailingPass] = field(default_factory=dict)
    commander_completed: dict[RecordedThreadIncarnation, float] = field(default_factory=dict)
    """Last observation completed for each commander, including quiet observations."""

    def failure_lines(self) -> tuple[str, ...]:
        return tuple(failure.failure_line(watch) for watch, failure in sorted(self.failing.items()))

    def heartbeat_completed_at(self, commander: RecordedThreadIncarnation) -> EventTime:
        """The original completion clock, or its declared unrecorded state."""
        if commander in self.commander_completed:
            return RecordedEventTime(self.commander_completed[commander])
        return UnrecordedEventTime()

    def heartbeat_current(self, heartbeat: EventTime) -> bool:
        """Only the commander's completion clock determines heartbeat freshness."""
        return heartbeat.since(time.time() - CommanderPass.completion_ceiling())


@dataclass(frozen=True)
class ServiceReading(ToolResult):
    """Read-time service evidence shared by board, load and independent edit replies."""

    services: Annotated[ServicePasses, WireTransient()] = field(
        default_factory=ServicePasses, compare=False, kw_only=True)
    heartbeat: Annotated[EventTime, WireTransient()] = field(
        default_factory=UnrecordedEventTime, compare=False, kw_only=True)

    def with_services(self, passes: ServicePasses, owner: RecordedThreadIncarnation) -> Self:
        return replace(self, services=passes, heartbeat=passes.heartbeat_completed_at(owner))

    @projected(view=WireExclude)
    def service_status(self) -> tuple[str, ...]:
        clock = self.heartbeat.aged_line('Tristan last completed', time.time())
        failures = self.services.failure_lines()
        if not failures and not self.services.heartbeat_current(self.heartbeat):
            failures = ('no failure recorded, so no heartbeat is running',)
        return (*failures, *(clock or ('Tristan last completed: unrecorded',)))

    def require_current(self) -> None:
        """A disclosure without a health header cannot serve stale cards silently."""
        if not self.services.heartbeat_current(self.heartbeat):
            raise BoardUnavailable(self, 'the commander heartbeat is stale')


class BoardUnavailable(ToolRefusedError):
    """A missing or mismatched publication retains the service evidence that explains it."""

    def __init__(self, reading: ServiceReading, reason: str) -> None:
        self.reading = reading
        super().__init__('\n'.join((*reading.service_status, f'Board unavailable: {reason}')))


@dataclass(frozen=True, slots=True)
class ServicePassStore(SingleFileStore, DurableFile, LockedStore[ServicePasses], DependentStateTier,
                       Watched):
    """The connector's log reaches nobody; the commander's standing rules read this file."""

    filename: ClassVar[str] = "service_passes.json"
    record_type = ServicePasses

    def empty(self) -> ServicePasses:
        return ServicePasses()

    def failed(self, watch: str, error: BaseException) -> None:
        def change(passes: ServicePasses) -> DocumentChange[ServicePasses, ServicePasses]:
            now = time.time()
            since = passes.failing[watch].since if watch in passes.failing else now
            failing = FailingPass(
                since=since, error=f"{type(error).__name__}: {error}", failed_at=now)
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
