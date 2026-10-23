"""Owner process lifecycle, maintenance and launch pin ownership."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from uuid import uuid4


from .active_route import resolve_comms_route
from agent_comms.attempt_recovery import RecoveryMonitorCapability
from .child_process import (
    ObservedProcess,
    OutlivesLauncherLifetime,
    Platform,
    ProcessIdentity,
    ProcessState,
    SpawnedProcess,
    SynchronousProcess,
)
from .cli_result import CliResult
from .commander_heartbeat import CommanderHeartbeat
from .diagnostics import OwnerProcessOutput
from .errors import RelationViolationError
from .launch_root import LaunchRoot
from .input_disposition import InputDispositions
from .launch_runtime import LaunchRuntime, RestartEnvironment, SetVariable, ThreadLaunch
from .maintenance_barrier import MaintenanceBarrier
from .message_bus import MessageBus
from .native_arguments import (
    ConfiguredBinary,
    InheritedArguments,
    LaunchArguments,
    LaunchBinary,
    NativeArguments,
)
from .private_nk_entrypoint import PrivateNkLaunch, RootLaunch
from .registration import Registration
from .registry_document import RegistrySnapshot
from .restart_refusals import (
    OwnerGenerationChangedRefusal,
    OwnerSelectionChangedRefusal,
)
from .restart_scope import AllOwners, RestartScope
from .thread_identity import ClaimedIdentity, OwnerIdentity, ThreadIncarnationRecord
from .thread_owned_state import ThreadOwnedState
from .thread_text import GivenThreadText, NoThreadText, ThreadText
from .threads import Thread
from .tool_result import ToolResult
from .typed_table import StoreRows, Column, DurableStore, JsonStorage, TypedTable

if TYPE_CHECKING:
    from .goal_management import Goals
    from .messaging import Messaging

_LOG = logging.getLogger(__name__)


class LaunchInput:
    def thread_text(self) -> ThreadText:
        """No first input, so the thread records no original assignment."""
        return NoThreadText()

    def launch(self, launch: ThreadLaunch) -> ThreadLaunch:
        return launch

    def record_first_input(self, record) -> None:
        pass


@dataclass(frozen=True)
class TaskLaunchInput(LaunchInput):
    text: str = field()
    input_key: str = field(default_factory=lambda: f"acp:{uuid4().hex}", init=False)

    def thread_text(self) -> GivenThreadText:
        return GivenThreadText(self.text)

    def launch(self, launch: ThreadLaunch) -> ThreadLaunch:
        return replace(launch, startup_input=SetVariable(self.input_key))

    def record_first_input(self, record) -> None:
        record(self.input_key, seq=None, text=self.text)


@dataclass(frozen=True, slots=True)
class OwnerRestartSelection:
    identity: OwnerIdentity
    process: ProcessIdentity
    claimed_generation: int
    interpreter: str

    @classmethod
    def capture(cls, snapshot: RegistrySnapshot, name: str):
        owner = snapshot.require_active(name)
        process = owner.require_process()
        platform = Platform.current()
        platform.require(process)
        command = Path(f"/proc/{process.pid}/cmdline").read_bytes().split(b"\0", 1)[0]
        interpreter = os.fsdecode(command)
        if not interpreter:
            raise RelationViolationError("Selected source owner interpreter is unavailable")
        platform.require(process)
        return cls(
            snapshot.owner_identity(owner.name), process,
            snapshot.claim_generation(owner.name), interpreter,
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
        if snapshot.claim_generation(self.name) != self.claimed_generation:
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
    def require_owner_loss(self, snapshot, owner_identity): ...

    def require_native_loss(self, snapshot, source):
        release = self.require_owner_loss(snapshot, source.owner_identity)
        current = snapshot.require(release.name)
        source.sent_owner_claimed_generation.require_release(release, snapshot, current)
        return release

    def require_stopping_owner(self, snapshot: RegistrySnapshot, thread: Thread,
                               claimed_generation: int) -> None:
        if self.current(snapshot, thread, claimed_generation):
            return
        try:
            snapshot.require_stopping_owner(thread, claimed_generation)
        except RelationViolationError as error:
            raise RelationViolationError(
                f"Owner changed while stopping {thread.name!r}; refusing a stale signal."
            ) from error


class NoRelease(OwnerRelease):
    def current(self, snapshot, expected, claim_scope) -> bool:
        return False

    def matches(self, expected) -> bool:
        return False

    def require_owner_loss(self, snapshot, owner_identity):
        raise RelationViolationError("native owner release acknowledgement is missing")


@dataclass(frozen=True)
class ReleasedOwner(ThreadIncarnationRecord):
    process_identity: Annotated[ProcessState, Column(storage=JsonStorage, nullable=True)]

    def require_process(self) -> ProcessIdentity:
        return self.process_identity.require(f"Thread {self.name!r} has no released process")

    def matches(self, expected: Thread) -> bool:
        return self.incarnation() == expected.incarnation and self.process_identity == expected.process_identity


class OwnerReleaseStoreRows(StoreRows):
    """The tables of OwnerReleaseStore; each row declares itself one."""


@dataclass(frozen=True)
class OwnerReleaseEvidence(OwnerReleaseStoreRows, ReleasedOwner, OwnerRelease, TypedTable):
    name: Annotated[str, Column(primary_key=True)]
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
        return self.after == snapshot.claim_generation(self.name) and snapshot.status(self.name).stopped

    def require_owner_loss(self, snapshot, owner_identity):
        """Attest only this recorded owner's release and dead process.

        Input generation belongs to the recorded input, not to owner-loss proof.
        Neither this release nor missing input proves bytes were unwritten.
        """
        if not self.incarnation().current(snapshot):
            raise RelationViolationError("Released native owner incarnation changed")
        current = snapshot.require(self.name)
        owner_identity.require_recorded_owner(current)
        process = self.require_process()
        if process.alive():
            raise RelationViolationError("Released native owner process is still alive")
        if self.matches(current):
            snapshot.status(current.name).require_inactive(
                "Released native process is still the registry owner")
        actual = snapshot.claimed_identity(current.name)
        released = ClaimedIdentity(self.incarnation(), self.after)
        if not actual.includes(released):
            raise RelationViolationError("Native release claim_scope regressed")
        if actual == released and not self.matches(current):
            raise RelationViolationError(
                "Native release process differs from its original declaration"
            )
        return self


@dataclass(frozen=True)
class OwnerReleaseStore(ThreadOwnedState, DurableStore):
    root: Path
    filename = "owner_releases.sqlite3"
    tables = OwnerReleaseStoreRows

    @contextmanager
    def reading(self) -> Iterator[Callable[[str], OwnerRelease]]:
        with self.snapshot() as db:
            def read(name: str) -> OwnerRelease:
                with closing(OwnerReleaseEvidence.stream(db, name=name)) as records:
                    return next(records, NoRelease())
            yield read

    def read_many(self, names: Sequence[str]) -> dict[str, OwnerRelease]:
        with self.reading() as read:
            return {name: read(name) for name in names}

    def record(self, snapshot: RegistrySnapshot, owners: Sequence[tuple[Thread, int]]) -> None:
        if not owners:
            return
        with self.writing() as db:
            for original, before in owners:
                thread = snapshot.require(original.name)
                thread.turn_record.read().require_idle()
                OwnerReleaseEvidence(thread.name, thread.incarnation.wire_birth,
                                     thread.process_identity, before,
                                     snapshot.claim_generation(thread.name)).upsert(db)

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

    def __init__(self, root: Path, registry: Registration, bus: MessageBus,
                 goals: Goals, messaging: Messaging):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.goals = goals
        self.messaging = messaging
        self.maintenance = MaintenanceBarrier(registry.store.path)
        self.releases = OwnerReleaseStore(root)

    def acquire_thread(self, name: str) -> Thread:
        """Bind only this calling process; stored birth time is signal authority."""
        identity = ProcessIdentity.capture(os.getpid())
        self.maintenance.assert_open_unlocked()
        thread = self.registry.require(name)
        thread.execution.require_native()
        thread.role.require_executable(
            RelationViolationError("A human participant cannot become an executor.")
        )
        if thread.process_alive:
            return thread
        return self._register_owner(thread, identity)

    def _register_owner(
        self, previous: Thread, identity: ProcessIdentity,
        initial_input: LaunchInput = LaunchInput(),
    ) -> Thread:
        """Reserve one owner and its first input before releasing the child's start gate."""
        owner = self.registry.reserve_owner(previous, identity)
        owned = owner.thread
        initial_input.record_first_input(partial(
            InputDispositions(self.root).record, owner=owned.name, target=owned.name,
            claim_scope=owner.claimed_generation,
        ))
        if previous.process_identity.offline():
            orders = tuple(
                todo
                for commander in CommanderHeartbeat.commanders(self.registry, self.goals)
                for todo in self.goals.todos.held_by(commander.incarnation, owned.incarnation)
                if todo.pending and todo.held().owner_identity == owned.incarnation
            )
            if orders:
                identities = ", ".join(sorted({
                    order.id for todo in orders for order in todo.order.declarations
                }))
                self._announce_restart(owned.name, identities)
        return owned

    def _announce_restart(self, name: str, identities: str) -> None:
        """Publish once after the registry's process reservation commits."""
        CommanderHeartbeat.register(self.registry)
        self.messaging.send(CommanderHeartbeat.sender, name,
            f"Restarted holding {identities}; continue them from your branch")

    def ensure_owner(
        self, name: str, *, agent_bin: str = NativeArguments.default_binary,
        agent_args: LaunchArguments = InheritedArguments(),
    ) -> Thread:
        with self.registry.storage_rule.room_for_start(self.registry.require(name), self.root):
            self.maintenance.assert_open_unlocked()
            thread = self.registry.require(name)
            thread.execution.require_native()
            refusal = RelationViolationError("Explicit start is required for a stopped agent.")
            thread.role.require_executable(refusal)
            self.registry.status(thread.name).require_active(str(refusal))
            return (
                thread
                if thread.process_alive
                else self._launch_owner(thread, agent_bin, agent_args,
                                                 launch_runtime=LaunchRuntime.running())
            )

    def start(
        self, name: str, *, agent_bin: LaunchBinary = ConfiguredBinary(),
        agent_args: LaunchArguments = InheritedArguments(),
    ) -> OwnerStartResult:
        with self.registry.storage_rule.room_for_start(self.registry.require(name), self.root):
            self.maintenance.assert_open_unlocked()
            thread = self.registry.require(name)
            thread.execution.require_native()
            status = self.registry.status(thread.name)
            refusal = RelationViolationError("Only visible agent threads can be started.")
            thread.role.require_executable(refusal)
            status.require_visible(str(refusal))
            if thread.process_alive:
                status.require_active("Stopped owner has not exited yet.")
                return OwnerStartResult(thread.name, thread.require_process().pid, False)
            owner = self._launch_owner(
                thread, agent_bin.configured(), agent_args,
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
        scope: RestartScope = AllOwners(),
        *,
        agent_bin: LaunchBinary = ConfiguredBinary(),
        agent_args: LaunchArguments = InheritedArguments(),
        expected: OwnerRestartSelection | None = None,
        runtime: RestartEnvironment | None = None,
        cutover: OwnerCutover = PreserveOwnerRuntime(),
    ) -> tuple[OwnerRestartResult, ...]:
        """Retain one batch through stop, optional quiet maintenance and launch.

        The declared update runs after every original exits and before any
        replacement launches. It owns its writer
        proof and operation; failure leaves the fenced batch stopped for review.
        """
        from .owner_restart import OwnerRestartRequest

        request = OwnerRestartRequest(scope, agent_bin, agent_args, expected, runtime)
        return cutover.restart(self, request)

    @contextmanager
    def _signal_guard(self, thread: Thread, claimed_generation: int):
        with self.registry.store.locked():
            self._require_same_stop_owner(thread, claimed_generation)
            yield

    def _stop_processes(self, owners: Sequence[tuple[Thread, int]]) -> None:
        """Retire every owner's process together, each signalled under its own guard."""
        stops = tuple((ObservedProcess(thread.require_process()),
                       partial(self._signal_guard, thread, generation))
                      for thread, generation in owners)
        try:
            SynchronousProcess.stop_together(stops)
        except ProcessLookupError:
            pass
        except ExceptionGroup as group:
            _, failed = group.split(ProcessLookupError)
            if failed is not None:
                raise failed

    def _stop_selection(self, name: str) -> tuple[Thread, int] | None:
        """Settle an owner that needs no signal, or capture the one that does."""
        with self.registry.store.locked():
            snapshot = self.registry.snapshot()
            thread = self.registry.require(name)
            thread.role.require_executable(
                RelationViolationError("Only agent threads can be stopped.")
            )
            if snapshot.status(thread.name).settle_local_stop(self, thread):
                return None
            return thread, snapshot.claim_generation(thread.name)

    def _settle_local_owner(self, thread: Thread) -> None:
        """Settle this process under the caller's existing stop-selection lock."""
        if ThreadLaunch.called_by(
            thread.name, canonicalize=lambda caller: self.registry.require(caller).name,
        ):
            self._release_current_owner_unlocked(thread.name)
        else:
            self.registry.unregister(thread.name)

    def stop(self, *names: str) -> None:
        owners = tuple(owner for name in dict.fromkeys(names)
                       if (owner := self._stop_selection(name)) is not None)
        # A process may already be dead. It still needs the exact release
        # acknowledgement before the execution owner can settle its attempted input.
        self._stop_processes(tuple(owner for owner in owners if owner[0].process_alive))
        # Stop is explicit abandonment of this exited local owner, not replay
        # permission. The existing recovery owner checks its exact release and
        # native-child absence before terminal settlement.
        self._finish_stopped_owners(owners)
        RecoveryMonitorCapability.settle_stopped_owners(
            self, tuple(thread for thread, _ in owners))

    def require_private_launch(self) -> PrivateNkLaunch:
        """The launch authority of a root that must be private."""
        return self.launch_authority.require_private()

    def require_launch_authority(self) -> RootLaunch:
        """Check this lifecycle's launch authority without reserving a process.

        A preserving restart can check before fencing; each actual process
        reservation rechecks the same declaration after cutover.
        """
        self.launch_authority.check()
        return self.launch_authority

    def _launch_owner(
        self,
        thread: Thread,
        agent_bin: str,
        agent_args: LaunchArguments = InheritedArguments(),
        *,
        launch_runtime: LaunchRuntime,
        initial_input: LaunchInput = LaunchInput(),
        environment: Mapping[str, str] | None = None,
    ) -> Thread:
        # Retire the predecessor while its released declaration is still stopped.
        # Reserving the replacement PID first destroys that observation.
        RecoveryMonitorCapability.recover_owners(self, (thread,))
        self.maintenance.assert_open_unlocked()
        env = dict(os.environ if environment is None else environment)
        launch_runtime.require_available()
        env = launch_runtime.environment(env)
        agent_bin = self.require_launch_authority().prepare_owner(thread, agent_bin, launch_runtime)
        env = initial_input.launch(thread.launch()).apply(env)
        env[NativeArguments.binary_key] = agent_bin
        agent_args.apply(env)
        with OwnerProcessOutput.open(self.root, thread) as output, SpawnedProcess.launching(
            (str(launch_runtime.interpreter), "-m", "agent_comms.worker",
             *LaunchRoot.from_path(self.launch_authority.child_root).arguments()),
            env=env,
            cwd=thread.worktree,
            output=output,
            runtime=launch_runtime,
            lifetime=OutlivesLauncherLifetime,
        ) as child:
            return self._register_owner(thread, child.identity, initial_input)

    def _require_same_stop_owner(self, thread: Thread, claimed_generation: int) -> None:
        snapshot = self.registry.snapshot()
        # Voluntary release advances the input generation and marks STOPPED before Python
        # finishes shutting down. Its exact acknowledgement preserves the signaled
        # incarnation; it does not prove OS exit or authorize a replacement PID.
        release = self.releases.read_many((thread.name,))[thread.name]
        release.require_stopping_owner(snapshot, thread, claimed_generation)

    def _finish_stopped_owners(self, owners: Sequence[tuple[Thread, int]]) -> None:
        if not owners:
            return
        with self.registry.store.editing() as edit:
            snapshot = edit.original.snapshot
            releases = self.releases.read_many(tuple(thread.name for thread, _ in owners))
            retiring = []
            for thread, generation in owners:
                release = releases[thread.name]
                if release.current(snapshot, thread, generation):
                    continue  # The original owner durably acknowledged its own release.
                release.require_stopping_owner(snapshot, thread, generation)
                retiring.append((thread, generation))
                edit.document.unregister(thread.name)
            edit.commit()
            self.releases.record(edit.original.snapshot, retiring)

    def release(self, name: str) -> None:
        """Let the calling participant mark itself stopped without signalling."""
        caller = ThreadLaunch.current().name
        with self.registry.store.locked():
            canonical = self.registry.require(name).name
            if self.registry.require(caller).name != canonical:
                raise RelationViolationError(f"Thread {caller!r} cannot release {canonical!r}.")
            self._release_current_owner_unlocked(canonical)

    def _release_current_owner_unlocked(self, canonical: str) -> None:
        snapshot = self.registry.snapshot()
        owner = snapshot.threads[canonical]
        owner.process_identity.require_release_by(ProcessIdentity.capture(os.getpid()))
        before = snapshot.claim_generation(canonical)
        self._retire_owner_unlocked(owner, before)

    def _retire_owner_unlocked(self, thread: Thread, before: int) -> None:
        """Share retirement only after the caller's distinct identity checks."""
        self.registry.unregister(thread.name)
        after = self.registry.snapshot()
        self.releases.record(after, ((thread, before),))
