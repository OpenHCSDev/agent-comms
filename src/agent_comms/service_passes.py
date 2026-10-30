"""Original completion times and current failures of Core's service watches."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from typing import Annotated, ClassVar, Self

from .coordinator import Coordination
from .locked_store import DocumentChange, DurableFile, LockedStore
from .errors import ToolRefusedError
from .field_codec import WireExclude, WireTransient, projected
from .tool_result import ToolResult
from .recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from .store_files import DependentStateTier, SingleFileStore
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .watched import FileSource, Watched


class CompletedWatch:
    """A service watch whose ordinary completion an install waits on after a restart; each
    member declares how long its longest pass takes."""

    pass_duration: ClassVar[float]

    @classmethod
    def completion_ceiling(cls) -> float:
        """How long a new runtime may take to record its first completion: one cadence (a pass,
        then the service loop's sleep) plus one pass of slack."""
        return 2 * cls.pass_duration + Coordination.watch_period


@dataclass(frozen=True)
class FailingPass:
    """A watch failing since its first failed pass, with its newest error and the time of its
    newest failed pass."""

    since: float
    error: str
    failed_at: float

    def failed_since(self, moment: float) -> bool:
        """Its newest pass failed at or after ``moment``; an older failure is a runtime's that
        ``moment`` replaced, and the next pass either clears or rewrites it."""
        return self.failed_at >= moment

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
        return heartbeat.since(time.time() - 3 * Coordination.watch_period)

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

    async def wait_completed(
        self, commanders: tuple[RecordedThreadIncarnation, ...], since: float,
        watch: type[CompletedWatch],
    ) -> None:
        """Join ordinary recorded completions, never run the watch being qualified.

        Subscribe before reading, so a completion between the first read and wait
        cannot disappear. A partial pass plus a recorded failure is not acceptance.
        """
        pending = commanders
        timeout = asyncio.timeout(watch.completion_ceiling())
        name = watch.__name__
        try:
            async with timeout:
                async with aclosing(self.watch_sources(FileSource(self.path))) as changes:
                    async for _ in changes:
                        passes = await Coordination.run_worker(self.read)
                        if name in passes.failing and passes.failing[name].failed_since(since):
                            raise ToolRefusedError(passes.failing[name].failure_line(name))
                        pending = tuple(owner for owner in commanders
                                        if not passes.completed_since(owner, since))
                        if not pending:
                            return
        except TimeoutError as error:
            if not timeout.expired():
                raise
            raise ToolRefusedError(
                f"{name} has no ordinary completion since service restart {since}: "
                + ", ".join(owner.name for owner in pending)
            ) from error

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
