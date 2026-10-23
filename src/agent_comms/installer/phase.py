"""One clock accounts for an install, including nested and concurrent operations."""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock

from agent_comms.declared_family import DeclaredFamily


@dataclass(frozen=True)
class InstallPhase(DeclaredFamily, affix="Phase"):
    """A declared operation measures its own run through the install's shared clock."""

    detail: str = ""

    def label(self) -> str:
        return f"{type(self).__name__}: {self.detail}" if self.detail else type(self).__name__

    @contextmanager
    def running(self, clock: InstallClock) -> Iterator[PhaseTiming]:
        with clock.lock:
            started = clock.advance()
            measured = PhaseTiming(self)
            clock.phases.append(measured)
            clock.active.append(measured)
        try:
            yield measured
        finally:
            with clock.lock:
                measured.elapsed_seconds = clock.advance() - started
                clock.active.remove(measured)


class InstallRunPhase(InstallPhase):
    """The whole installer, including teardown and its final publication."""


class PreparePhase(InstallPhase):
    """Read the selected target, outgoing runtime and pending operation declarations."""


class BuildPhase(InstallPhase):
    """Resolve and build the target stack and its native package."""


class CommandPhase(InstallPhase):
    """An external command, identified by its actual invocation."""


class GitPhase(InstallPhase):
    """A Git operation through the shared repository owner."""


class ProbePhase(InstallPhase):
    """A declared operation answered by an interpreter worker."""


class PracticePhase(InstallPhase):
    """Select and rehearse the incoming changes on their practice bus."""


class CopyPhase(InstallPhase):
    """Copy and retain the practice inputs selected by their file owners."""


class CarryPhase(InstallPhase):
    """Run a declared carry operation in its target interpreter."""


class VerifyPhase(InstallPhase):
    """Read back the carried bus and compare the files the steps changed."""


class LivePhase(InstallPhase):
    """Stop owners, carry the held bus, publish and restart its captured owners."""


class PublicationPhase(InstallPhase):
    """Publish the route, runtime or install result through its existing owner."""


class WorkerClosePhase(InstallPhase):
    """Close and reap the install's persistent interpreter workers."""


class TreeClosePhase(InstallPhase):
    """Retire the target worktree and the install's scratch directory."""


@dataclass(eq=False)
class PhaseTiming:
    phase: InstallPhase
    seconds: float = 0.0
    """Exclusive wall time: overlapping work is charged once, to the last active phase."""
    elapsed_seconds: float = 0.0
    """The phase's own elapsed run, including its nested or concurrent work."""

    def line(self) -> str:
        return f"{self.seconds:.3f}s {self.phase.label()} ({self.elapsed_seconds:.3f}s elapsed)"


@dataclass
class InstallClock:
    """The single timing record and active execution stack shared by all install workers.

    Each phase boundary advances the same clock. Exclusive seconds partition the
    run, rather than adding a parent's duration to its children's durations. A
    concurrent operation shares that wall interval; its elapsed_seconds still
    records its full duration. There is no per-command or per-copy timer.
    """

    phases: list[PhaseTiming] = field(default_factory=list)
    active: list[PhaseTiming] = field(default_factory=list, init=False)
    lock: RLock = field(default_factory=RLock, init=False)
    previous: float = field(default_factory=time.monotonic, init=False)

    def advance(self) -> float:
        now = time.monotonic()
        if self.active:
            self.active[-1].seconds += now - self.previous
        self.previous = now
        return now
