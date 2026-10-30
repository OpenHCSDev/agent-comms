"""Dependency-light resource timing values; never send or readiness authority."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field


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

    def operation(self, name: str) -> AbstractContextManager[None]:
        """Borrow bounded counters for a declared acquisition/publication operation.

        These counters observe resources; they never decide readiness, acknowledgement
        acceptance or replay. Owners call this with their fixed operation names.
        """
        return self.operations.setdefault(name, PublicationMeasurements()).measuring()

    @contextmanager
    def measuring(self) -> Iterator[None]:
        started = time.monotonic_ns()
        try:
            yield
        finally:
            finished = time.monotonic_ns()
            duration = finished - started
            if self.count == 0 or started < self.first_started_ns:
                self.first_started_ns = started
            self.last_finished_ns = max(self.last_finished_ns, finished)
            self.count += 1
            self.total_ns += duration
            if duration > self.maximum_ns:
                self.maximum_ns = duration
                self.maximum_started_ns, self.maximum_finished_ns = started, finished
