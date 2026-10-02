"""Acquired retained restart phases, shared by admission and target launch.

An original-format process may execute these phases with its own registry
declarations. Only the stopped handoff crosses runtimes; it has no Thread or
RegistryDocument to reinterpret. Production readers keep one format.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import stat
from typing import TYPE_CHECKING

from .errors import RelationViolationError
from .owner_launch import RestartEnvironment, RetainedOwnerLaunch
from .restart_refusals import OwnerBusyRefusal, OwnerSelectionChangedRefusal
from .store_files import StoreLock, _store_lock
from .thread_identity import AdmissionIdentity, OwnerIdentity
from .owner_lifecycle import OwnerRestartSelection

if TYPE_CHECKING:
    from .owner_cutover import OwnerCutover
    from .owner_lifecycle import OwnerLifecycle, OwnerRestartResult
    from .registry_document import RegistrySnapshot
    from .threads import Thread


@dataclass(frozen=True)
class OwnerRestartRequest:
    names: tuple[str, ...] | None = None
    agent_bin: str | None = None
    agent_args: tuple[str, ...] | None = None
    expected: OwnerRestartSelection | None = None
    runtime: RestartEnvironment | None = None
    source_interpreter: str | None = None

    def threads(self, snapshot: RegistrySnapshot) -> list[Thread]:
        if self.names is None:
            return [candidate for thread in snapshot.threads.values()
                    for candidate in thread.execution.restart_candidates(
                        thread, snapshot.statuses[thread.name])]
        return list({snapshot.require_active(name).name: snapshot.require_active(name)
                     for name in self.names}.values())

    def require_selection(self, snapshot: RegistrySnapshot, threads: list[Thread]) -> None:
        if self.expected is not None:
            if len(threads) != 1 or threads[0].name != self.expected.name:
                raise OwnerSelectionChangedRefusal()
            self.expected.require_current(snapshot)

    def environment(self, lifecycle: OwnerLifecycle) -> RestartEnvironment:
        return RestartEnvironment.inherit(lifecycle.restart_environment(
            os.environ if self.runtime is None else self.runtime.encode()))


@dataclass(frozen=True)
class RetiredOwnerLaunch:
    """Original stopped registry observation plus acquired launch resources."""

    owner: OwnerIdentity
    admission: AdmissionIdentity
    launch: RetainedOwnerLaunch

    @property
    def name(self) -> str:
        return self.owner.incarnation.name

    def require_current(self, snapshot: RegistrySnapshot) -> Thread:
        if snapshot.owner_identity(self.name) != self.owner:
            raise RelationViolationError('Retired owner incarnation/generation changed')
        if snapshot.admission_identity(self.name) != self.admission:
            raise RelationViolationError('Retired owner admission changed')
        thread = snapshot.threads[self.name]
        thread.require_local_process(self.launch.process)
        thread.require_idle()
        snapshot.statuses[self.name].require_stopped()
        if thread.process_alive:
            raise RelationViolationError('Original owner survived retirement')
        return thread


@dataclass(frozen=True)
class OwnerRestartHandoff:
    """Bounded original custody, transferred once; never persisted credentials."""

    root: str
    owners: tuple[RetiredOwnerLaunch, ...]
    runtime: RestartEnvironment
    agent_bin: str | None
    agent_args: tuple[str, ...] | None

    def launch(self, lifecycle: OwnerLifecycle) -> tuple[OwnerRestartResult, ...]:
        from .owner_lifecycle import OwnerRestartResult

        if str(lifecycle.root) != self.root:
            raise RelationViolationError('Retained batch belongs to another root')
        snapshot = lifecycle.registry.snapshot()
        # Close the entire witness set before ANY replacement launch.
        threads = tuple(owner.require_current(snapshot) for owner in self.owners)
        return tuple(OwnerRestartResult(
            thread.name, owner.launch.process.pid,
            lifecycle._launch_owner_unlocked(
                thread, self.agent_bin or lifecycle.restart_entrypoint(owner.launch.binary),
                self.agent_args if self.agent_args is not None else owner.launch.arguments,
                environment=self.runtime.apply_runtime(owner.launch.environment),
            ).pid,
        ) for thread, owner in zip(threads, self.owners, strict=True))


@dataclass(frozen=True)
class StoppedOwnerBatch:
    """Only this acquired phase can finish installation and resume the batch."""

    lifecycle: OwnerLifecycle
    wire: StoreLock
    handoff: OwnerRestartHandoff

    @classmethod
    def accept(cls, lifecycle: OwnerLifecycle, descriptor: int, handoff: OwnerRestartHandoff):
        """Accept an ORIGINAL inherited wire OFD; never reacquire it by name."""
        import fcntl

        opened = os.fstat(descriptor)
        named = (lifecycle.root / '.wire.lock').stat()
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid():
            raise RelationViolationError('Retained wire descriptor is not owned storage')
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise RelationViolationError('Retained wire descriptor names another lock')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if str(lifecycle.root) != handoff.root:
            raise RelationViolationError('Retained stopped batch names another root')
        return cls(lifecycle, StoreLock(descriptor, None), handoff)

    def launch(self) -> tuple[OwnerRestartResult, ...]:
        return self.handoff.launch(self.lifecycle)


@dataclass(frozen=True)
class FencedOwnerBatch:
    lifecycle: OwnerLifecycle
    captured: tuple[tuple[Thread, int], ...]
    launches: tuple[RetainedOwnerLaunch, ...]
    request: OwnerRestartRequest
    runtime: RestartEnvironment

    def complete(self, cutover: OwnerCutover) -> tuple[OwnerRestartResult, ...]:
        for thread, generation in self.captured:
            self.lifecycle._stop_process(thread, generation)
        with _store_lock(self.lifecycle.root / 'wire') as wire:
            for thread, generation in self.captured:
                self.lifecycle._require_same_stop_owner(thread, generation)
                if thread.process_alive:
                    raise RelationViolationError('Owner survived retirement')
            snapshot = self.lifecycle.registry.snapshot()
            owners = tuple(RetiredOwnerLaunch(
                snapshot.owner_identity(thread.name), snapshot.admission_identity(thread.name), launch,
            ) for (thread, _), launch in zip(self.captured, self.launches, strict=True))
            handoff = OwnerRestartHandoff(str(self.lifecycle.root), owners, self.runtime,
                                         self.request.agent_bin, self.request.agent_args)
            return cutover.complete(StoppedOwnerBatch(self.lifecycle, wire, handoff))


@dataclass(frozen=True)
class AdmittedOwnerBatch:
    lifecycle: OwnerLifecycle
    captured: tuple[tuple[Thread, int], ...]
    launches: tuple[RetainedOwnerLaunch, ...]
    request: OwnerRestartRequest
    runtime: RestartEnvironment

    @classmethod
    def restart(cls, lifecycle: OwnerLifecycle, request: OwnerRestartRequest,
                cutover: OwnerCutover) -> tuple[OwnerRestartResult, ...]:
        with _store_lock(lifecycle.root / 'wire'):
            lifecycle.maintenance.assert_open_unlocked()
            snapshot = lifecycle.registry.snapshot()
            threads = request.threads(snapshot)
            request.require_selection(snapshot, threads)
            cutover.require_selection(snapshot, threads)
            captured = []
            for thread in threads:
                thread.execution.require_native()
                generation = snapshot.admission_generations[thread.name]
                thread.role.require_executable()
                snapshot.statuses[thread.name].require_active()
                if thread.require_process().pid == os.getpid():
                    raise RelationViolationError('Restart requires another live owner')
                try:
                    thread.require_idle()
                except RelationViolationError as error:
                    raise OwnerBusyRefusal() from error
                captured.append((thread, generation))
            runtime = request.environment(lifecycle)
            launches = tuple(RetainedOwnerLaunch.capture(
                thread, snapshot, interpreter=request.source_interpreter,
            ) for thread, _ in captured)
            admitted = cls(lifecycle, tuple(captured), launches, request, runtime)
            fenced = admitted.fence()
        return fenced.complete(cutover)

    def fence(self) -> FencedOwnerBatch:
        captured = self.lifecycle.registry.fence_idle_owners(self.captured)
        return FencedOwnerBatch(self.lifecycle, captured, self.launches, self.request, self.runtime)
