"""Owner process lifecycle, maintenance and launch pin ownership."""

from __future__ import annotations

import logging
import os
import shlex
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from uuid import uuid4

from .child_process import ObservedProcess, ParentedProcess, ProcessIdentity, ProcessState
from .turn_lease import NoTurnRecord
from .cli_result import CliResult
from .tool_result import ToolResult
from .diagnostics import owner_process_output
from .errors import RelationViolationError
from .maintenance_barrier import MaintenanceBarrier
from .message_bus import MessageBus
from .native_arguments import NativeArguments
from .launch_runtime import LaunchRuntime, RestartEnvironment, ThreadLaunch
from .active_route import resolve_comms_route
from .private_nk_entrypoint import PrivateNkLaunch, RootLaunch
from .registration import Registration
from .registry_document import RegistrySnapshot
from .restart_refusals import (
    OwnerGenerationChangedRefusal,
    OwnerSelectionChangedRefusal,
)
from .store_files import _store_lock
from .thread_identity import ClaimedIdentity, OwnerIdentity, ThreadIncarnationRecord
from .thread_owned_state import ThreadOwnedState
from .threads import Thread
from .typed_table import Column, JsonStorage, TypedStore, TypedTable

_LOG = logging.getLogger(__name__)


class LaunchInput:
    text = ""

    def launch(self, launch: ThreadLaunch) -> ThreadLaunch:
        return launch

    def record_first_input(self, record) -> None:
        pass


@dataclass(frozen=True)
class TaskLaunchInput(LaunchInput):
    text: str = field()
    input_key: str = field(default_factory=lambda: f"acp:{uuid4().hex}", init=False)

    def launch(self, launch: ThreadLaunch) -> ThreadLaunch:
        return replace(launch, startup_input=self.input_key)

    def record_first_input(self, record) -> None:
        record(self.input_key, seq=None, text=self.text)


@dataclass(frozen=True, slots=True)
class OwnerRestartSelection:
    identity: OwnerIdentity
    process: ProcessIdentity
    claimed_generation: int = field(metadata={"wire_name": "admission_generation"})

    @classmethod
    def capture(cls, snapshot: RegistrySnapshot, name: str):
        owner = snapshot.require_active(name)
        return cls(
            snapshot.owner_identity(owner.name),
            owner.require_process(),
            snapshot.claimed_generations[owner.name],
        )

    @classmethod
    def live(cls, selections, snapshot) -> tuple[OwnerRestartSelection, ...]:
        """Retain only the exact selected processes that have not stopped."""
        living = []
        for selection in selections:
            if selection.process.alive():
                selection.require_current(snapshot)
                living.append(selection)
        return tuple(living)

    @property
    def name(self) -> str:
        return self.identity.incarnation.name

    def require_current(self, snapshot: RegistrySnapshot) -> Thread:
        try:
            snapshot.require_owner_process(self.identity, self.process)
        except RelationViolationError as error:
            raise OwnerSelectionChangedRefusal() from error
        if snapshot.claimed_generations[self.name] != self.claimed_generation:
            raise OwnerGenerationChangedRefusal()
        return snapshot.require_active(self.name)


@dataclass(frozen=True, slots=True)
class OwnerRestartResult:
    thread: str
    previous_pid: int
    pid: int


@dataclass(frozen=True, slots=True)
class OwnerStartResult(CliResult, ToolResult):
    thread: str
    pid: int
    launched: bool

    def reconnect_targets(self) -> tuple[str, ...]:
        return (self.thread,) if self.launched else ()


class OwnerRelease(ABC):
    """An exact release or its absence; neither grants permission to replay."""
    @abstractmethod
    def current(self, snapshot, expected, claim_scope) -> bool: ...
    @abstractmethod
    def matches(self, expected) -> bool: ...
    @abstractmethod
    def require_native_loss(self, snapshot, source): ...


class NoRelease(OwnerRelease):
    def current(self, snapshot, expected, claim_scope) -> bool:
        return False

    def matches(self, expected) -> bool:
        return False

    def require_native_loss(self, snapshot, source):
        raise RelationViolationError("native owner release acknowledgement is missing")


@dataclass(frozen=True)
class ReleasedOwner(ThreadIncarnationRecord):
    process_identity: ProcessState = field(
        metadata={"sql": Column(storage=JsonStorage, nullable=True)},
    )

    def require_process(self) -> ProcessIdentity:
        return self.process_identity.require(f"Thread {self.name!r} has no released process")

    def matches(self, expected: Thread) -> bool:
        return self.incarnation() == expected.incarnation and self.process_identity == expected.process_identity


@dataclass(frozen=True)
class OwnerReleaseEvidence(ReleasedOwner, OwnerRelease, TypedTable):
    name: str = field(metadata={"sql": Column(primary_key=True)})
    before: int
    after: int

    def __post_init__(self) -> None:
        if self.before <= 0 or self.after <= self.before:
            raise ValueError("Owner release requires an advanced claim_scope and a closed turn")

    def current(self, snapshot: RegistrySnapshot, expected: Thread, claim_scope: int) -> bool:
        if self.before != claim_scope or not self.matches(expected):
            return False
        if not self.incarnation().current(snapshot) or not self.matches(snapshot.require(self.name)):
            return False
        return self.after == snapshot.claimed_generations[self.name] and snapshot.statuses[self.name].stopped


    def require_native_loss(self, snapshot, source) -> None:
        """A recorded release confirms only its exact dead process and input generation.

        A recorded send can be fenced by a later release in the same incarnation.
        An unrecorded epoch instead needs the exact current stopped acknowledgement. Both
        paths preserve UNKNOWN; neither says bytes were unwritten or permits retry.
        """
        if not self.incarnation().current(snapshot):
            raise RelationViolationError("Released native owner incarnation changed")
        current = snapshot.require(self.name)
        source.require_recorded_owner(current)
        process = self.require_process()
        if process.alive():
            raise RelationViolationError("Released native owner process is still alive")
        actual = snapshot.claimed_identity(current.name)
        released = ClaimedIdentity(self.incarnation(), self.after)
        if not actual.includes(released):
            raise RelationViolationError("Native release claim_scope regressed")
        source.sent_owner_claimed_generation.require_release(self, snapshot, current)
        return self


@dataclass(frozen=True)
class OwnerReleaseStore(ThreadOwnedState, TypedStore):
    root: Path
    filename = "owner_releases.sqlite3"
    rows = (OwnerReleaseEvidence,)

    def get(self, name: str) -> OwnerRelease:
        with self.snapshot() as db, closing(OwnerReleaseEvidence.stream(db, name=name)) as records:
            return next(records, NoRelease())

    def record(self, thread: Thread, before: int, after: int) -> None:
        thread.turn_record.read().require_idle()
        with self.writing() as db:
            OwnerReleaseEvidence(thread.name, thread.created_at, thread.process_identity, before, after).upsert(db)

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        with self.writing() as db:
            for thread in threads:
                OwnerReleaseEvidence.delete(db, name=thread.name)


from .owner_cutover import OwnerCutover, PreserveOwnerRuntime


class OwnerLifecycle:
    def require_restart_idle(self, thread: Thread) -> None:
        """Do not interrupt a live turn; an old attempt settles after owner release."""
        from .restart_refusals import OwnerBusyRefusal

        try:
            thread.turn_record.read().require_idle()
        except RelationViolationError as error:
            raise OwnerBusyRefusal() from error

    def __init__(self, root: Path, registry: Registration, bus: MessageBus):
        self.root = root
        self.registry = registry
        self.bus = bus
        self._wire_lock_path = root / "wire"
        self.maintenance = MaintenanceBarrier(registry.store.path)
        self.releases = OwnerReleaseStore(root)

    @contextmanager
    def restart_wire(self) -> Iterator[int]:
        """Borrow this declaration's wire lock as the inherited integer OFD.

        StoreLock continues to own current durability resources. Restart phases
        transport only its descriptor; the context retains the original lock.
        """
        with _store_lock(self._wire_lock_path) as lock:
            yield lock.descriptor

    def acquire_thread(self, name: str, *, owner_pid: int) -> Thread:
        """Bind only this calling process; stored birth time is signal authority."""
        if owner_pid != os.getpid():
            raise RelationViolationError("An owner may acquire only its own process identity.")
        identity = ProcessIdentity.capture(owner_pid)
        with _store_lock(self._wire_lock_path):
            self.maintenance.assert_open_unlocked()
            thread = self.registry.require(name)
            thread.execution.require_native()
            thread.role.require_executable(
                RelationViolationError("A human participant cannot become an executor.")
            )
            if thread.process_alive:
                return thread
            owned = replace(thread, process_identity=identity, turn_record=NoTurnRecord())
            self.registry.register(owned, new_owner=True)
            return owned

    def ensure_owner(
        self, name: str, *, agent_bin: str = NativeArguments.default_binary,
        agent_args: Sequence[str] | None = None,
    ) -> Thread:
        with (self.registry.storage_rule.room_for_start(self.registry.require(name), self.root),
              _store_lock(self._wire_lock_path)):
            self.maintenance.assert_open_unlocked()
            thread = self.registry.require(name)
            thread.execution.require_native()
            refusal = RelationViolationError("Explicit start is required for a stopped agent.")
            thread.role.require_executable(refusal)
            if not self.registry.status(thread.name).active:
                raise refusal
            return (
                thread
                if thread.process_alive
                else self._launch_owner_unlocked(thread, agent_bin, agent_args,
                                                 launch_runtime=LaunchRuntime.running())
            )

    def start(
        self, name: str, *, agent_bin: str | None = None, agent_args: Sequence[str] | None = None
    ) -> OwnerStartResult:
        with (self.registry.storage_rule.room_for_start(self.registry.require(name), self.root),
              _store_lock(self._wire_lock_path)):
            self.maintenance.assert_open_unlocked()
            thread = self.registry.require(name)
            thread.execution.require_native()
            status = self.registry.status(thread.name)
            refusal = RelationViolationError("Only visible agent threads can be started.")
            thread.role.require_executable(refusal)
            if not status.visible:
                raise refusal
            if thread.process_alive:
                if not status.active:
                    raise RelationViolationError("Stopped owner has not exited yet.")
                return OwnerStartResult(thread.name, thread.require_process().pid, False)
            owner = self._launch_owner_unlocked(
                thread, NativeArguments.configured_binary(agent_bin), agent_args,
                launch_runtime=LaunchRuntime.running(),
            )
            return OwnerStartResult(owner.name, owner.require_process().pid, True)

    @cached_property
    def launch_authority(self) -> RootLaunch:
        """This root's launch, derived once per process (RootLaunch.derive).

        The root is the one this lifecycle serves and is never guessed.
        Acquisition verifies the immutable package once; check() still
        refuses a replaced root.
        """
        return resolve_comms_route(self.root).launch()

    def restart_entrypoint(self, source_binary: str, runtime: LaunchRuntime) -> str:
        """Choose this runtime's reviewed native entrypoint for a private handoff."""
        return self.launch_authority.restart_entrypoint(source_binary, runtime)

    def restart_environment(self, environment: Mapping[str, str], runtime: LaunchRuntime) -> dict[str, str]:
        """Project the launch authority into the target runtime policy."""
        result = runtime.environment(environment)
        # The source owner's root is this launch's; the target derives the rest.
        self.launch_authority.check()
        return result

    def restart_owners(
        self,
        names: Sequence[str] | None = None,
        *,
        agent_bin: str | None = None,
        agent_args: Sequence[str] | None = None,
        expected: OwnerRestartSelection | None = None,
        runtime: RestartEnvironment | None = None,
        source_interpreter: str | None = None,
        cutover: OwnerCutover = PreserveOwnerRuntime(),
    ) -> tuple[OwnerRestartResult, ...]:
        """Retain one batch through stop, optional quiet maintenance and launch.

        The declared update runs under the wire input lock after every
        original exits and before any replacement launches. It owns its writer
        proof and operation; failure leaves the fenced batch stopped for review.
        """
        from .owner_restart import OwnerRestartRequest

        request = OwnerRestartRequest(
            tuple(names) if names is not None else None, agent_bin,
            tuple(agent_args) if agent_args is not None else None,
            expected, runtime, source_interpreter,
        )
        return cutover.restart(self, request)

    @contextmanager
    def _signal_guard(self, thread: Thread, claimed_generation: int):
        with _store_lock(self._wire_lock_path):
            self._require_same_stop_owner(thread, claimed_generation)
            yield

    def _stop_process(self, thread: Thread, claimed_generation: int) -> None:
        identity = thread.require_process()
        with suppress(ProcessLookupError):
            ObservedProcess(identity).stop_sync(
                guard=lambda: self._signal_guard(thread, claimed_generation),
            )

    def stop(self, name: str) -> None:
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            thread = self.registry.require(name)
            if not snapshot.statuses[thread.name].active and (
                thread.process_identity.allows_inactive_stop()
            ):
                return
            if (
                snapshot.statuses[thread.name].active
                and thread.process_identity.runs_here()
            ):
                if ThreadLaunch.called_by(
                    thread.name,
                    canonicalize=lambda caller: self.registry.require(caller).name,
                ):
                    self._release_current_owner_unlocked(thread.name)
                else:
                    self.registry.unregister(thread.name)
                return
            generation = snapshot.claimed_generations[thread.name]
        # The process may already be dead. It still needs the exact release
        # acknowledgement before the execution owner can settle its attempted input.
        if thread.process_alive:
            self._stop_process(thread, generation)
        self._finish_stopped_owner(thread, generation)
        # Stop is explicit abandonment of this exited local owner, not replay
        # permission. The existing recovery owner checks its exact release and
        # native-child absence before terminal settlement.
        from .attempt_recovery import RecoveryMonitorCapability

        RecoveryMonitorCapability.settle_stopped_owner(self.root, thread)

    def require_private_launch(self) -> PrivateNkLaunch:
        """The launch authority of a root that must be private."""
        return self.launch_authority.require_private()

    def require_launch_authority(self) -> RootLaunch:
        """Check this lifecycle's launch authority without reserving a process.

        Callers hold the wire lock. A preserving restart can check before
        fencing; every actual launch rechecks the same owner after cutover.
        """
        self.launch_authority.check()
        return self.launch_authority

    def _launch_owner_unlocked(
        self,
        thread: Thread,
        agent_bin: str,
        agent_args: Sequence[str] | None = None,
        *,
        launch_runtime: LaunchRuntime,
        initial_input: LaunchInput = LaunchInput(),
        environment: Mapping[str, str] | None = None,
    ) -> Thread:
        # Callers hold the wire lock; a phase change takes wire then registry.
        self.maintenance.assert_open_unlocked()
        env = dict(os.environ if environment is None else environment)
        launch_runtime.require_available()
        env = launch_runtime.environment(env)
        agent_bin = self.require_launch_authority().prepare_owner(thread, agent_bin, launch_runtime)
        env = initial_input.launch(thread.launch()).apply(env)
        env[NativeArguments.binary_key] = agent_bin
        env["AGENT_COMMS_ROOT"] = str(self.launch_authority.child_root)
        if agent_args is not None:
            env[NativeArguments.arguments_key] = shlex.join(agent_args)
        owned: Thread | None = None

        def reserve(identity: ProcessIdentity) -> None:
            nonlocal owned
            owned = replace(thread, process_identity=identity, turn_record=NoTurnRecord())
            self.registry.register(owned, new_owner=True)

        with owner_process_output(self.root, thread) as output:
            ParentedProcess.launch(
                (str(launch_runtime.interpreter), "-m", "agent_comms.worker"),
                env=env,
                cwd=thread.worktree,
                output=output,
                before_start=reserve,
                runtime=launch_runtime,
            )
        assert owned is not None
        return owned

    def _require_same_stop_owner(self, thread: Thread, claimed_generation: int) -> None:
        snapshot = self.registry.snapshot()
        # Voluntary release advances the input generation and marks STOPPED before Python
        # finishes shutting down. Its exact acknowledgement preserves the signaled
        # incarnation; it does not prove OS exit or authorize a replacement PID.
        if self.releases.get(thread.name).current(snapshot, thread, claimed_generation):
            return
        try:
            snapshot.require_stopping_owner(thread, claimed_generation)
        except RelationViolationError as error:
            raise RelationViolationError(
                f"Owner changed while stopping {thread.name!r}; refusing a stale signal."
            ) from error

    def _finish_stopped_owner(self, thread: Thread, claimed_generation: int) -> None:
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            if self.releases.get(thread.name).current(snapshot, thread, claimed_generation):
                return  # An exact, durably attested release of the signaled owner.
            self._require_same_stop_owner(thread, claimed_generation)
            self._retire_owner_unlocked(thread, claimed_generation)

    def release(self, name: str) -> None:
        """Let the calling participant mark itself stopped without signalling."""
        caller = ThreadLaunch.current().name
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.require(name).name
            if self.registry.require(caller).name != canonical:
                raise RelationViolationError(f"Thread {caller!r} cannot release {canonical!r}.")
            self._release_current_owner_unlocked(canonical)

    def _release_current_owner_unlocked(self, canonical: str) -> None:
        snapshot = self.registry.snapshot()
        owner = snapshot.threads[canonical]
        owner.process_identity.require_release_by(ProcessIdentity.capture(os.getpid()))
        before = snapshot.claimed_generations[canonical]
        self._retire_owner_unlocked(owner, before)

    def _retire_owner_unlocked(self, thread: Thread, before: int) -> None:
        """Share retirement only after the caller's distinct identity checks."""
        self.registry.unregister(thread.name)
        after = self.registry.snapshot()
        self.releases.record(after.threads[thread.name], before, after.claimed_generations[thread.name])
