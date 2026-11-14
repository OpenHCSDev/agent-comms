"""Dependency-light resource timing values; never send or readiness authority."""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from functools import partial, wraps
from typing import ClassVar


@dataclass
class PublicationMeasurements:
    """Bounded resource timings, not a message, phase or successful-send authority.

    First/last delimit observed bodies, including exceptional exits. Maxima and
    totals remain counters; nested operations must not be added as disjoint work.
    """
    count: int = 0
    total_ns: int = 0
    maximum_ns: int = 0
    maximum_started_ns: int = 0
    maximum_finished_ns: int = 0
    first_started_ns: int = 0
    last_finished_ns: int = 0
    operations: dict[str, PublicationMeasurements] = field(default_factory=dict)

    clock: ClassVar[Callable[[], int]] = time.monotonic_ns
    """What one observed body is measured in: elapsed time here."""

    def named(self, name: str) -> PublicationMeasurements:
        return self.operations.setdefault(name, type(self)())

    def operation(self, name: str) -> AbstractContextManager[None]:
        """Borrow bounded counters for a declared acquisition/publication operation.

        These counters observe resources; they never decide readiness, acknowledgement
        acceptance or replay. Owners call this with their fixed operation names.
        """
        return self.named(name).measuring()

    @contextmanager
    def measuring(self) -> Iterator[None]:
        started = self.clock()
        try:
            yield
        finally:
            self.observed(started, self.clock())

    def observed(self, started: int, finished: int) -> None:
        duration = finished - started
        if self.count == 0 or started < self.first_started_ns:
            self.first_started_ns = started
        self.last_finished_ns = max(self.last_finished_ns, finished)
        self.count += 1
        self.total_ns += duration
        if duration > self.maximum_ns:
            self.maximum_ns = duration
            self.maximum_started_ns, self.maximum_finished_ns = started, finished


@dataclass
class ThreadCpuMeasurements(PublicationMeasurements):
    """The same counters in CPU time of the thread that ran the body. Its first and last
    marks are that thread's own CPU clock, so only counts, totals and maxima compare."""

    operations: dict[str, ThreadCpuMeasurements] = field(default_factory=dict)

    clock: ClassVar[Callable[[], int]] = time.thread_time_ns


class WorkLedger:
    """What this process spent in each kind of joined work since it started, by the name of
    the function a caller handed over. Each member measures one quantity at one door; a
    turn's diagnostics carry the running totals, so the difference between two turns' rows
    is the work between them."""

    ledger: ClassVar[PublicationMeasurements]
    changing: ClassVar[threading.Lock] = threading.Lock()

    @staticmethod
    def name_of(operation: Callable[..., object]) -> str:
        """The innermost function a caller handed over, under the partials that bound it."""
        while isinstance(operation, partial):
            operation = operation.func
        return getattr(operation, "__qualname__", type(operation).__qualname__)

    @classmethod
    def observed(cls, name: str, started: int, finished: int) -> None:
        with cls.changing:
            cls.ledger.named(name).observed(started, finished)

    @classmethod
    def totals(cls) -> PublicationMeasurements:
        with cls.changing:
            return copy.deepcopy(cls.ledger)


class WorkCpu(WorkLedger):
    """Thread CPU of each joined operation. Every blocking operation a turn hands to a worker
    thread or to the coordinator thread passes one of two doors, so measuring there names
    where a turn's CPU went without attaching anything to the running process."""

    ledger: ClassVar[ThreadCpuMeasurements] = ThreadCpuMeasurements()

    @classmethod
    def measured[**P, Result](cls, operation: Callable[P, Result]) -> Callable[P, Result]:
        name = cls.name_of(operation)
        clock = cls.ledger.clock

        @wraps(operation)
        def run(*args: P.args, **kwargs: P.kwargs) -> Result:
            started = clock()
            try:
                return operation(*args, **kwargs)
            finally:
                cls.observed(name, started, clock())

        return run


class CoordinatorWait(WorkLedger):
    """Elapsed time each coordinator operation waited for the coordinator thread after its
    caller handed it over: one thread runs every agent's store work in turn, so this is the
    queue behind everything handed over before it."""

    ledger: ClassVar[PublicationMeasurements] = PublicationMeasurements()


class CoordinatorRun(WorkLedger):
    """Elapsed time each coordinator operation ran on the coordinator thread. Less its
    WorkCpu, it is what the operation waited on there: locks, the disk, the GIL."""

    ledger: ClassVar[PublicationMeasurements] = PublicationMeasurements()
