"""Retained restart phases shared by permission checks and target launch.

An original-format process may execute these phases with its own registry
declarations. Only the stopped handoff crosses runtimes; it has no Thread or
RegistryDocument to reinterpret. Production readers keep one format.
"""
from __future__ import annotations

import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from .coordination_errors import CoordinationError
from .errors import RelationViolationError
from .owner_launch import RetainedOwnerLaunch
from .launch_runtime import LaunchRuntime, RestartEnvironment, RestoredRuntime
from .owner_lifecycle import OwnerRestartSelection
from .restart_refusals import OwnerSelectionChangedRefusal
from .store_files import store_lock_path
from .thread_identity import ClaimedIdentity, OwnerIdentity

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
    launch_runtime: LaunchRuntime = field(default_factory=LaunchRuntime.running, kw_only=True)

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
        self.launch_runtime.require_available()
        return RestartEnvironment.inherit(lifecycle.restart_environment(
            os.environ if self.runtime is None else self.runtime.encode(), self.launch_runtime))


@dataclass(frozen=True)
class RetiredOwnerLaunch:
    """Original stopped registry observation plus acquired launch resources."""

    owner: OwnerIdentity
    claim_scope: ClaimedIdentity = field(metadata={"wire_name": 'admission'})
    launch: RetainedOwnerLaunch

    @classmethod
    def capture_retired(cls, snapshot: RegistrySnapshot, original: Thread,
                        launch: RetainedOwnerLaunch) -> RetiredOwnerLaunch:
        """Capture the original allocations after stopping, without permitting live work."""
        original.require_local_process(launch.process)
        owner = cls(snapshot.owner_identity(original.name),
                    snapshot.claimed_identity(original.name), launch)
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
        if snapshot.claimed_identity(self.name) != self.claim_scope:
            raise RelationViolationError('Retired owner claim_scope changed')
        thread = snapshot.threads[self.name]
        thread.require_local_process(self.launch.process)
        thread.turn_record.read().require_idle()
        snapshot.statuses[self.name].require_stopped()
        if thread.process_alive:
            raise RelationViolationError('Original owner survived retirement')
        return thread


@dataclass(frozen=True)
class OwnerRestartHandoff:
    """Captured owner launches passed to the next phase once; never saved credentials."""

    root: str
    owners: tuple[RetiredOwnerLaunch, ...]
    runtime: RestartEnvironment
    agent_bin: str | None
    agent_args: tuple[str, ...] | None
    launch_runtime: LaunchRuntime

    def launch(self, lifecycle: OwnerLifecycle, runtime: LaunchRuntime) -> tuple[OwnerRestartResult, ...]:
        def target(thread, source):
            return lifecycle._launch_owner_unlocked(
                thread, self.agent_bin or lifecycle.restart_entrypoint(source.binary, runtime),
                self.agent_args if self.agent_args is not None else source.arguments,
                environment=self.runtime.apply_runtime(source.environment),
                launch_runtime=runtime,
            )

        return self._launch(lifecycle, target)

    def restore(self, lifecycle: OwnerLifecycle) -> tuple[OwnerRestartResult, ...]:
        """Restore EACH acquired launch unchanged under its authentic decoder."""
        runtime = RestoredRuntime.running()
        if any(owner.launch.interpreter != str(runtime.interpreter) for owner in self.owners):
            raise RelationViolationError('Original restoration requires the acquired source interpreter')

        return self._launch(lifecycle, lambda thread, source: lifecycle._launch_owner_unlocked(
            thread, source.binary, source.arguments, environment=source.environment,
            launch_runtime=runtime,
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
            launch(thread, owner.launch).require_process().pid,
        ) for thread, owner in zip(threads, self.owners, strict=True))


@dataclass(frozen=True)
class StoppedOwnerBatch:
    """Only this acquired phase can finish installation and resume the batch."""

    lifecycle: OwnerLifecycle
    wire_descriptor: int
    handoff: OwnerRestartHandoff
    ownership: ExitStack = field(default_factory=ExitStack, repr=False, compare=False)

    @classmethod
    def accept(cls, lifecycle: OwnerLifecycle, descriptor: int, handoff: OwnerRestartHandoff):
        """Accept an ORIGINAL inherited wire OFD; never reacquire it by name."""
        import fcntl

        if type(descriptor) is not int or descriptor < 0:
            raise RelationViolationError('Retained wire requires an integer descriptor')
        opened = os.fstat(descriptor)
        named = store_lock_path(lifecycle.root / 'wire').stat()
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid():
            raise RelationViolationError('Retained wire descriptor is not owned storage')
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise RelationViolationError('Retained wire descriptor names another lock')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if str(lifecycle.root) != handoff.root:
            raise RelationViolationError('Retained stopped batch names another root')
        return cls(lifecycle, descriptor, handoff)

    def launch(self, runtime: LaunchRuntime) -> tuple[OwnerRestartResult, ...]:
        return self.handoff.launch(self.lifecycle, runtime)

    def close(self) -> None:
        """Release held locks and descriptors; never launch or retry inputs."""
        self.ownership.close()

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
            operation.failed(StoppedOwnerFailure(self, operation, cause))
        self.close()
        return results


class StoppedOwnerFailure(CoordinationError):
    """A failed fenced/stopped phase, not an unsignalled restart refusal."""

    def __init__(
        self, stopped: StoppedOwnerBatch | FencedOwnerBatch, operation: OwnerCutover,
        cause: BaseException,
    ):
        super().__init__(
            'Cutover failed after fencing; retirement ownership requires explicit disposition: '
            f'{type(cause).__name__}: {cause}'
        )
        self.__cause__ = cause
        self.stopped = stopped
        self.operation = operation

    def recover(self) -> tuple[OwnerRestartResult, ...]:
        """Use the same restart operation to recover processes while its state is in memory."""
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
    ownership: ExitStack = field(default_factory=ExitStack, repr=False, compare=False)

    @property
    def unconfirmed(self) -> tuple[tuple[Thread, int], ...]:
        """No stopped witness for these originals, even if OS exit was observed."""
        names = {owner.name for owner in self.retired}
        return tuple(item for item in self.captured if item[0].name not in names)

    def close(self) -> None:
        self.ownership.close()

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
                self.lifecycle._finish_stopped_owner(thread, generation)
                progress = replace(progress, exited_processes=progress.exited_processes + (launch,))
                # The next original may need this same lock to release itself.
                # Capture each retired witness, then release before its stop.
                with self.lifecycle.restart_wire():
                    self.lifecycle._require_same_stop_owner(thread, generation)
                    snapshot = self.lifecycle.registry.snapshot()
                    owner = RetiredOwnerLaunch.capture_retired(snapshot, thread, launch)
                progress = replace(progress, retired=progress.retired + (owner,))
                # The retired owner has the only proof that its attempted input
                # cannot keep running. Settle through the execution owner before
                # a replacement can claim work; an unresolved publication blocks
                # this cutover rather than granting replay or dropping the pointer.
                from .attempt_recovery import RecoveryMonitorCapability

                RecoveryMonitorCapability.settle_stopped_owner(self.lifecycle.root, thread)
            wire = self.ownership.enter_context(self.lifecycle.restart_wire())
            snapshot = self.lifecycle.registry.snapshot()
            owners = progress.retired
            progress = replace(progress, retired=())
            for owner in owners:
                owner.require_current(snapshot)
                progress = replace(progress, retired=progress.retired + (owner,))
            handoff = OwnerRestartHandoff(str(self.lifecycle.root), progress.retired, self.runtime,
                                         self.request.agent_bin, self.request.agent_args,
                                         self.request.launch_runtime)
            stopped = StoppedOwnerBatch(self.lifecycle, wire, handoff, self.ownership.pop_all())
        except BaseException as cause:
            cutover.failed(StoppedOwnerFailure(progress, cutover, cause))
        return stopped.complete(cutover)


@dataclass(frozen=True)
class ClaimedOwnerBatch:
    lifecycle: OwnerLifecycle
    captured: tuple[tuple[Thread, int], ...]
    launches: tuple[RetainedOwnerLaunch, ...]
    request: OwnerRestartRequest
    runtime: RestartEnvironment

    @classmethod
    def restart(cls, lifecycle: OwnerLifecycle, request: OwnerRestartRequest,
                cutover: OwnerCutover) -> tuple[OwnerRestartResult, ...]:
        with lifecycle.restart_wire():
            cutover.require_restart_phase(lifecycle)
            snapshot = lifecycle.registry.snapshot()
            threads = request.threads(snapshot)
            request.require_selection(snapshot, threads)
            cutover.require_selection(snapshot, threads)
            captured = []
            for thread in threads:
                thread.require_restart_owner(snapshot.statuses[thread.name])
                generation = snapshot.claimed_generations[thread.name]
                lifecycle.require_restart_idle(thread)
                captured.append((thread, generation))
            runtime = request.environment(lifecycle)
            launches = tuple(RetainedOwnerLaunch.capture(
                thread, snapshot, interpreter=request.source_interpreter,
            ) for thread, _ in captured)
            if captured:
                cutover.require_launch(lifecycle)
            authorized = cls(lifecycle, tuple(captured), launches, request, runtime)
            fenced = authorized.fence()
        return fenced.complete(cutover)

    def fence(self) -> FencedOwnerBatch:
        captured = self.lifecycle.registry.fence_idle_owners(self.captured)
        return FencedOwnerBatch(self.lifecycle, captured, self.launches, self.request, self.runtime)
