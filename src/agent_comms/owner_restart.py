"""Retained restart phases shared by permission checks and target launch.

An original-format process may execute these phases with its own registry
declarations. Only the stopped handoff crosses runtimes; it has no Thread or
RegistryDocument to reinterpret. Production readers keep one format.
"""
from __future__ import annotations

import os
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from .coordination_errors import CoordinationError
from .errors import RelationViolationError
from .launch_runtime import LaunchRuntime, RestartEnvironment, RestoredRuntime
from .native_arguments import (
    ConfiguredBinary,
    GivenArguments,
    InheritedArguments,
    LaunchArguments,
    LaunchBinary,
)
from .owner_launch import RetainedOwnerLaunch
from .owner_lifecycle import OwnerRestartSelection
from .restart_refusals import OwnerSelectionChangedRefusal
from .restart_scope import AllOwners, RestartScope
from .thread_identity import ThreadAuthority

if TYPE_CHECKING:
    from .owner_cutover import OwnerCutover
    from .owner_lifecycle import OwnerLifecycle, OwnerRestartResult
    from .registry_document import RegistrySnapshot
    from .threads import Thread


@dataclass(frozen=True)
class OwnerRestartRequest:
    scope: RestartScope = AllOwners()
    agent_bin: LaunchBinary = ConfiguredBinary()
    agent_args: LaunchArguments = InheritedArguments()
    expected: OwnerRestartSelection | None = None
    runtime: RestartEnvironment | None = None
    launch_runtime: LaunchRuntime = field(default_factory=LaunchRuntime.running, kw_only=True)

    def owner_selection(self, snapshot: RegistrySnapshot, owner: Thread) -> OwnerRestartSelection:
        return self.expected or OwnerRestartSelection.capture(snapshot, owner.name)

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

    authority: ThreadAuthority
    launch: RetainedOwnerLaunch

    @classmethod
    def capture_retired(cls, snapshot: RegistrySnapshot, original: Thread,
                        launch: RetainedOwnerLaunch) -> RetiredOwnerLaunch:
        """Capture the original allocations after stopping, without permitting live work."""
        original.require_local_process(launch.process)
        owner = cls(snapshot.authority(original.name), launch)
        if owner.authority.owner.incarnation != original.incarnation:
            raise RelationViolationError('Retired owner incarnation changed')
        owner.require_current(snapshot)
        return owner

    @property
    def name(self) -> str:
        return self.authority.owner.incarnation.name

    def require_current(self, snapshot: RegistrySnapshot) -> Thread:
        if snapshot.authority(self.name) != self.authority:
            raise RelationViolationError('Retired owner authority changed')
        thread = snapshot.threads[self.name]
        thread.require_local_process(self.launch.process)
        thread.turn_record.read().require_idle()
        snapshot.status(self.name).require_stopped()
        if thread.process_alive:
            raise RelationViolationError('Original owner survived retirement')
        return thread


@dataclass(frozen=True)
class OwnerRestartHandoff:
    """Captured owner launches passed to the next phase once; never saved credentials."""

    root: str
    owners: tuple[RetiredOwnerLaunch, ...]
    runtime: RestartEnvironment
    agent_bin: LaunchBinary
    agent_args: LaunchArguments
    launch_runtime: LaunchRuntime

    def launch(self, lifecycle: OwnerLifecycle, runtime: LaunchRuntime) -> tuple[OwnerRestartResult, ...]:
        def target(thread, source):
            return lifecycle._launch_owner(
                thread, self.agent_bin.restarted(lifecycle, source.binary, runtime),
                self.agent_args.restarted(source.arguments),
                environment=self.runtime.apply_runtime(source.environment),
                launch_runtime=runtime,
            )

        return self._launch(lifecycle, target)

    def restore(self, lifecycle: OwnerLifecycle) -> tuple[OwnerRestartResult, ...]:
        """Restore EACH acquired launch unchanged under its authentic decoder."""
        runtime = RestoredRuntime.running()
        if any(owner.launch.interpreter != str(runtime.interpreter) for owner in self.owners):
            raise RelationViolationError('Original restoration requires the acquired source interpreter')

        return self._launch(lifecycle, lambda thread, source: lifecycle._launch_owner(
            thread, source.binary, GivenArguments(source.arguments), environment=source.environment,
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
    handoff: OwnerRestartHandoff
    ownership: ExitStack = field(default_factory=ExitStack, repr=False, compare=False)

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
            # Retain the same batch and its installation resources. The
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
            # Every original shares one grace period instead of waiting in turn.
            self.lifecycle._stop_processes(self.captured)
            if any(thread.process_alive for thread, _ in self.captured):
                raise RelationViolationError('Owner survived retirement')
            self.lifecycle._finish_stopped_owners(self.captured)
            with self.lifecycle.registry.store.locked():
                snapshot = self.lifecycle.registry.snapshot()
                releases = self.lifecycle.releases.read_many(
                    tuple(thread.name for thread, _ in self.captured))
                for (thread, generation), launch in zip(self.captured, self.launches, strict=True):
                    releases[thread.name].require_stopping_owner(snapshot, thread, generation)
                    owner = RetiredOwnerLaunch.capture_retired(snapshot, thread, launch)
                    progress = replace(progress, retired=progress.retired + (owner,))
            # Each original's acquired witness excludes its input before any replacement.
            from .attempt_recovery import RecoveryMonitorCapability

            RecoveryMonitorCapability.settle_stopped_owners(
                self.lifecycle, tuple(thread for thread, _ in self.captured))
            self.ownership.enter_context(cutover.stopped_resources(self.lifecycle))
            snapshot = self.lifecycle.registry.snapshot()
            owners = progress.retired
            progress = replace(progress, retired=())
            for owner in owners:
                owner.require_current(snapshot)
                progress = replace(progress, retired=progress.retired + (owner,))
            handoff = OwnerRestartHandoff(str(self.lifecycle.root), progress.retired, self.runtime,
                                         self.request.agent_bin, self.request.agent_args,
                                         self.request.launch_runtime)
            stopped = StoppedOwnerBatch(self.lifecycle, handoff, self.ownership.pop_all())
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
        with lifecycle.registry.store.locked():
            cutover.require_restart_phase(lifecycle)
            snapshot = lifecycle.registry.snapshot()
            threads = request.scope.threads(snapshot)
            request.require_selection(snapshot, threads)
            cutover.require_selection(snapshot, threads)
            captured = []
            for thread in threads:
                thread.require_restart_owner(snapshot.status(thread.name))
                generation = snapshot.claim_generation(thread.name)
                lifecycle.require_restart_idle(thread)
                captured.append((thread, generation))
            runtime = request.environment(lifecycle)
            launches = tuple(RetainedOwnerLaunch.capture(
                cutover.owner_selection(snapshot, thread, request), snapshot,
            ) for thread, _ in captured)
            if captured:
                cutover.require_launch(lifecycle)
            authorized = cls(lifecycle, tuple(captured), launches, request, runtime)
            fenced = authorized.fence()
        return fenced.complete(cutover)

    def fence(self) -> FencedOwnerBatch:
        captured = self.lifecycle.registry.fence_idle_owners(self.captured)
        return FencedOwnerBatch(self.lifecycle, captured, self.launches, self.request, self.runtime)
