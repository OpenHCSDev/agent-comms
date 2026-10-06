"""Acquired retained restart phases, shared by admission and target launch.

An original-format process may execute these phases with its own registry
declarations. Only the stopped handoff crosses runtimes; it has no Thread or
RegistryDocument to reinterpret. Production readers keep one format.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field, replace
import os
import stat
import sys
from typing import TYPE_CHECKING

from .errors import RelationViolationError
from .owner_launch import RestartEnvironment, RetainedOwnerLaunch
from .restart_refusals import OwnerBusyRefusal, OwnerSelectionChangedRefusal
from .owner_lifecycle import OwnerRestartSelection
from .thread_identity import AdmissionIdentity, OwnerIdentity

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
                    for candidate in thread.restart_candidates(snapshot.statuses[thread.name])]
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

    @classmethod
    def capture_retired(cls, snapshot: RegistrySnapshot, original: Thread,
                        launch: RetainedOwnerLaunch) -> RetiredOwnerLaunch:
        """Acquire the original post-retirement allocations, never live admission."""
        original.require_local_process(launch.process)
        owner = cls(snapshot.owner_identity(original.name),
                    snapshot.admission_identity(original.name), launch)
        if owner.owner.incarnation != original.incarnation:
            raise RelationViolationError('Retired owner incarnation changed')
        owner.require_current(snapshot)
        return owner

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
        def target(thread, source):
            return lifecycle._launch_owner_unlocked(
                thread, self.agent_bin or lifecycle.restart_entrypoint(source.binary),
                self.agent_args if self.agent_args is not None else source.arguments,
                environment=self.runtime.apply_runtime(source.environment),
            )

        return self._launch(lifecycle, target)

    def restore(self, lifecycle: OwnerLifecycle) -> tuple[OwnerRestartResult, ...]:
        """Restore EACH acquired launch unchanged under its authentic decoder."""
        if any(owner.launch.interpreter != sys.executable for owner in self.owners):
            raise RelationViolationError('Original restoration requires the acquired source interpreter')

        return self._launch(lifecycle, lambda thread, source: lifecycle._launch_owner_unlocked(
            thread, source.binary, source.arguments, environment=source.environment,
        ))

    def _launch(self, lifecycle: OwnerLifecycle, launch) -> tuple[OwnerRestartResult, ...]:
        from .owner_lifecycle import OwnerRestartResult

        if str(lifecycle.root) != self.root:
            raise RelationViolationError('Retained batch belongs to another root')
        snapshot = lifecycle.registry.snapshot()
        # Close the entire witness set before ANY replacement launch.
        threads = tuple(owner.require_current(snapshot) for owner in self.owners)
        return tuple(OwnerRestartResult(
            thread.name, owner.launch.process.pid,
            launch(thread, owner.launch).pid,
        ) for thread, owner in zip(threads, self.owners, strict=True))


@dataclass(frozen=True)
class StoppedOwnerBatch:
    """Only this acquired phase can finish installation and resume the batch."""

    lifecycle: OwnerLifecycle
    wire_descriptor: int
    handoff: OwnerRestartHandoff
    custody: ExitStack = field(default_factory=ExitStack, repr=False, compare=False)

    @classmethod
    def accept(cls, lifecycle: OwnerLifecycle, descriptor: int, handoff: OwnerRestartHandoff):
        """Accept an ORIGINAL inherited wire OFD; never reacquire it by name."""
        import fcntl

        if type(descriptor) is not int or descriptor < 0:
            raise RelationViolationError('Retained wire requires an integer descriptor')
        opened = os.fstat(descriptor)
        named = (lifecycle.root / '.wire.lock').stat()
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid():
            raise RelationViolationError('Retained wire descriptor is not owned storage')
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise RelationViolationError('Retained wire descriptor names another lock')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if str(lifecycle.root) != handoff.root:
            raise RelationViolationError('Retained stopped batch names another root')
        return cls(lifecycle, descriptor, handoff)

    def launch(self) -> tuple[OwnerRestartResult, ...]:
        return self.handoff.launch(self.lifecycle)

    def close(self) -> None:
        """Explicitly release physical custody; never launch or retry inputs."""
        self.custody.close()

    def recover(self, operation: OwnerCutover) -> tuple[OwnerRestartResult, ...]:
        results = operation.recover(self)
        self.close()
        return results

    def complete(self, operation: OwnerCutover) -> tuple[OwnerRestartResult, ...]:
        try:
            results = operation.complete(self)
        except BaseException as cause:
            # Transfer the SAME batch and original opened wire resource. The
            # caller owns disposition before exiting; credentials stay in RAM.
            failure = StoppedOwnerFailure(self, operation)
            failure.__cause__ = cause
            operation.failed(failure)
        self.close()
        return results


class StoppedOwnerFailure(Exception):
    """A failed fenced/stopped phase, not an unsignalled restart refusal."""

    def __init__(self, stopped: StoppedOwnerBatch | FencedOwnerBatch, operation: OwnerCutover):
        super().__init__('Cutover failed after fencing; retirement custody requires explicit disposition')
        self.stopped = stopped
        self.operation = operation

    def recover(self) -> tuple[OwnerRestartResult, ...]:
        """The original operation alone can certify recovery while RAM survives."""
        return self.stopped.recover(self.operation)

    def abandon(self) -> None:
        """Leave the exact phase as observed; never signal or launch its owners."""
        self.stopped.close()


@dataclass(frozen=True)
class FencedOwnerBatch:
    lifecycle: OwnerLifecycle
    captured: tuple[tuple[Thread, int], ...]
    launches: tuple[RetainedOwnerLaunch, ...]
    request: OwnerRestartRequest
    runtime: RestartEnvironment
    exited_processes: tuple[RetainedOwnerLaunch, ...] = ()
    retired: tuple[RetiredOwnerLaunch, ...] = ()
    custody: ExitStack = field(default_factory=ExitStack, repr=False, compare=False)

    @property
    def unconfirmed(self) -> tuple[tuple[Thread, int], ...]:
        """No stopped witness for these originals, even if OS exit was observed."""
        names = {owner.name for owner in self.retired}
        return tuple(item for item in self.captured if item[0].name not in names)

    def close(self) -> None:
        self.custody.close()

    def recover(self, operation: OwnerCutover) -> tuple[OwnerRestartResult, ...]:
        # A partial fence has no all-stopped launch authority. In particular,
        # never pass changed/still-live owners to an installation's recovery.
        raise RelationViolationError('Fenced batch lacks a complete validated stopped handoff')

    def complete(self, cutover: OwnerCutover) -> tuple[OwnerRestartResult, ...]:
        progress = self
        try:
            for (thread, generation), launch in zip(self.captured, self.launches, strict=True):
                self.lifecycle._stop_process(thread, generation)
                if thread.process_alive:
                    raise RelationViolationError('Owner survived retirement')
                progress = replace(progress, exited_processes=progress.exited_processes + (launch,))
                # The next original may need this same lock to release itself.
                # Capture each retired witness, then release before its stop.
                with self.lifecycle.restart_wire():
                    self.lifecycle._require_same_stop_owner(thread, generation)
                    snapshot = self.lifecycle.registry.snapshot()
                    owner = RetiredOwnerLaunch.capture_retired(snapshot, thread, launch)
                progress = replace(progress, retired=progress.retired + (owner,))
            wire = self.custody.enter_context(self.lifecycle.restart_wire())
            snapshot = self.lifecycle.registry.snapshot()
            owners = progress.retired
            progress = replace(progress, retired=())
            for owner in owners:
                owner.require_current(snapshot)
                progress = replace(progress, retired=progress.retired + (owner,))
            handoff = OwnerRestartHandoff(str(self.lifecycle.root), progress.retired, self.runtime,
                                         self.request.agent_bin, self.request.agent_args)
            stopped = StoppedOwnerBatch(self.lifecycle, wire, handoff, self.custody.pop_all())
        except BaseException as cause:
            failure = StoppedOwnerFailure(progress, cutover)
            failure.__cause__ = cause
            cutover.failed(failure)
        return stopped.complete(cutover)


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
        with lifecycle.restart_wire():
            lifecycle.maintenance.assert_open_unlocked()
            snapshot = lifecycle.registry.snapshot()
            threads = request.threads(snapshot)
            request.require_selection(snapshot, threads)
            cutover.require_selection(snapshot, threads)
            captured = []
            for thread in threads:
                thread.require_restart_owner(snapshot.statuses[thread.name])
                generation = snapshot.admission_generations[thread.name]
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
