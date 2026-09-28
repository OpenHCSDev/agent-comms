"""Benchmark-only timing around real canonical locks; all barriers still execute."""

from __future__ import annotations

import fcntl
import math
import sys
import threading
import time
from abc import ABC, abstractmethod
from array import array
from contextlib import contextmanager
from dataclasses import dataclass, field

from agent_comms import store_files


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "p50_ms": ordered[math.ceil(len(ordered) * .50) - 1],
        "p99_ms": ordered[math.ceil(len(ordered) * .99) - 1],
        "max_ms": ordered[-1],
        "total_ms": sum(ordered),
    }


class ReadPolicy(ABC):
    @abstractmethod
    def shared(self, requested: bool) -> bool: ...

    @property
    def name(self):
        return type(self).__name__


class CurrentSharedReads(ReadPolicy):
    def shared(self, requested):
        return requested


class ForcedExclusiveReads(ReadPolicy):
    def shared(self, requested):
        return False


@dataclass
class LockSample:
    path: str
    requested_shared: bool
    effective_shared: bool
    started: float = field(default_factory=time.perf_counter)
    acquired: float | None = None
    raw_wait_ms: float = 0


class LockObservation:
    """Instrument canonical aliases and flock itself, restoring both after use.

    Full acquisition includes open/mkdir, raw wait and the production durability
    barrier. Hold begins at successful flock and ends after context exit/close;
    it includes the barrier and a small context-exit measurement overhead.
    No-op locks, omitted fsyncs and mocked store APIs are never used.
    """

    def __init__(self, policy: ReadPolicy, event_budget: int):
        self.policy = policy
        self.event_budget = event_budget
        self.events = 0
        self.rows = {}
        self.local = threading.local()
        self.original_lock = store_files._store_lock
        self.original_flock = fcntl.flock

    def __enter__(self):
        replacement = self.locked
        for module in tuple(sys.modules.values()):
            if module is None or not module.__name__.startswith("agent_comms"):
                continue
            for name, value in tuple(vars(module).items()):
                if value is self.original_lock:
                    setattr(module, name, replacement)
        fcntl.flock = self.flock
        return self

    def __exit__(self, *_):
        fcntl.flock = self.original_flock
        # Includes modules imported lazily while observation was active.
        for module in tuple(sys.modules.values()):
            if module is not None and module.__name__.startswith("agent_comms"):
                for name, value in tuple(vars(module).items()):
                    if value == self.locked:
                        setattr(module, name, self.original_lock)

    def flock(self, descriptor, operation):
        started = time.perf_counter()
        result = self.original_flock(descriptor, operation)
        acquired = time.perf_counter()
        sample = getattr(self.local, "sample", None)
        if sample is not None and operation & (fcntl.LOCK_SH | fcntl.LOCK_EX):
            sample.raw_wait_ms += (acquired - started) * 1000
            sample.acquired = acquired
        return result

    @contextmanager
    def locked(self, path, *, blocking=True, max_bus_bytes=None, shared=False):
        sample = LockSample(path.name, shared, self.policy.shared(shared))
        previous = getattr(self.local, "sample", None)
        self.local.sample = sample
        entered = None
        try:
            with self.original_lock(path, blocking=blocking, max_bus_bytes=max_bus_bytes,
                                    shared=sample.effective_shared) as descriptor:
                entered = time.perf_counter()
                yield descriptor
        finally:
            finished = time.perf_counter()
            self.local.sample = previous
            if entered is not None and sample.acquired is not None:
                self.events += 1
                if self.events > self.event_budget:
                    raise RuntimeError("Benchmark lock event budget exceeded")
                key = (sample.path, sample.requested_shared, sample.effective_shared,
                       threading.current_thread().name)
                metrics = self.rows.setdefault(key, {
                    name: array("d") for name in
                    ("raw_flock_wait", "acquisition_including_barrier", "hold_including_barrier")
                })
                metrics["raw_flock_wait"].append(sample.raw_wait_ms)
                metrics["acquisition_including_barrier"].append((entered - sample.started) * 1000)
                metrics["hold_including_barrier"].append((finished - sample.acquired) * 1000)

    def report(self):
        return [
            {"store": key[0], "requested_shared": key[1], "effective_shared": key[2],
             "thread": key[3], **{name: distribution(values) for name, values in metrics.items()}}
            for key, metrics in sorted(self.rows.items())
        ]
