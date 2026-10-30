"""Owner process lifecycle, maintenance and launch pin ownership."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated


from .active_route import resolve_comms_route
from agent_comms.attempt_recovery import RecoveryMonitorCapability
from .child_process import (
    Platform,
    ProcessIdentity,
    ProcessState,
)
from .cli_result import CliResult
from .declared_family import DeclaredFamily
from .owner_connection import OwnerConnection
from .owner_launch import RetainedOwnerLaunch, StartOwnerRuntimeRequest, StopOwnerRuntimeRequest
from .root_runtime import RootRuntimeStore
from .commander_service import CommanderService
from .channel_targets import ThreadTarget
from .task_attachments.owner_notices import RestartNotice
from .diagnostics import OwnerProcessOutput
from .errors import RelationViolationError
from .launch_root import LaunchRoot
from .input_key import AcpInputKey, InputKey
from .field_codec import InputId
from .input_disposition import InputDispositions
from .launch_runtime import INHERITED_ENVIRONMENT, LaunchRuntime, RestartEnvironment, SetVariable, ThreadLaunch
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
from .repo import Checkout
from .registry_document import RegistrySnapshot
from .registry_store import RegistryEdit
from .restart_refusals import (
    OwnerBusyRefusal,
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
    input_key: InputKey = field(default_factory=lambda: AcpInputKey(InputId.mint()), init=False)

    def thread_text(self) -> GivenThreadText:
        return GivenThreadText(self.text)

    def launch(self, launch: ThreadLaunch) -> ThreadLaunch:
        return replace(launch, startup_input=SetVariable(str(self.input_key)))

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
        interpreter = process.arguments()[0]
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
        return (self.thread,)


class OwnerRelease(ABC):
    """An exact release or its absence; neither grants permission to replay."""
    @abstractmethod
    def current(self, snapshot, expected, claim_scope) -> bool: ...
    @abstractmethod
    def matches(self, expected) -> bool: ...
    @abstractmethod
    def require_owner_loss(self, snapshot, owner_identity): ...

    def retirement(self, snapshot: RegistrySnapshot, original: Thread, before: int) -> OwnerCompletion:
        raise RelationViolationError("Completed owner retirement is missing")

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
        return self.incarnation == expected.incarnation and self.process_identity == expected.process_identity


class OwnerReleaseStoreRows(StoreRows):
    """The tables of OwnerReleaseStore; each row declares itself one."""


class OwnerCompletion(DeclaredFamily, affix="OwnerCompletion"):
    @abstractmethod
    def require_loss(self, snapshot: RegistrySnapshot, release: OwnerReleaseEvidence) -> None: ...

    @abstractmethod
    def require_retired(self, snapshot: RegistrySnapshot, thread: Thread,
                        process: ProcessIdentity) -> None: ...

    def matches_current(self, snapshot: RegistrySnapshot, release: OwnerReleaseEvidence) -> bool:
        return release.matches(snapshot.require(release.name))


@dataclass(frozen=True)
class ProcessExitedOwnerCompletion(OwnerCompletion):
    def require_retired(self, snapshot: RegistrySnapshot, thread: Thread,
                        process: ProcessIdentity) -> None:
        thread.require_local_process(process)
        if process.alive():
            raise RelationViolationError("Original owner survived retirement")

    def require_loss(self, snapshot: RegistrySnapshot, release: OwnerReleaseEvidence) -> None:
        if release.require_process().alive():
            raise RelationViolationError("Released native owner process is still alive")
        current = snapshot.require(release.name)
        if release.matches(current):
            snapshot.status(current.name).require_inactive("Released native process is still the registry owner")
        if snapshot.claim_generation(current.name) == release.after and not release.matches(current):
            raise RelationViolationError("Native release process differs from its original declaration")


@dataclass(frozen=True)
class SessionRetiredOwnerCompletion(OwnerCompletion):
    owner: OwnerIdentity

    def require_retired(self, snapshot: RegistrySnapshot, thread: Thread,
                        process: ProcessIdentity) -> None:
        current = snapshot.owner_identity(thread.name)
        if current.incarnation != self.owner.incarnation or current.generation <= self.owner.generation:
            raise RelationViolationError("Retired session still holds its original owner lease")
        if not thread.process_identity.allows_owner_start():
            raise RelationViolationError("Retired session acquired another process")

    def require_loss(self, snapshot: RegistrySnapshot, release: OwnerReleaseEvidence) -> None:
        if self.owner.incarnation != release.incarnation:
            raise RelationViolationError("Retired session belongs to another incarnation")
        if snapshot.owner_identity(release.name).generation <= self.owner.generation:
            raise RelationViolationError("Retired session still holds its original owner lease")

    def matches_current(self, snapshot: RegistrySnapshot, release: OwnerReleaseEvidence) -> bool:
        self.require_loss(snapshot, release)
        return release.incarnation.current(snapshot)


@dataclass(frozen=True)
class OwnerReleaseEvidence(OwnerReleaseStoreRows, ReleasedOwner, OwnerRelease, TypedTable):
    name: Annotated[str, Column(primary_key=True)]
    before: int
    after: int
    completion: Annotated[OwnerCompletion, Column()] = ProcessExitedOwnerCompletion()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.before <= 0 or self.after <= self.before:
            raise ValueError("Owner release requires an advanced claim_scope and a closed turn")

    def current(self, snapshot: RegistrySnapshot, expected: Thread, claim_scope: int) -> bool:
        if self.before != claim_scope or not self.matches(expected):
            return False
        if not self.incarnation.current(snapshot) or not self.completion.matches_current(snapshot, self):
            return False
        return self.after == snapshot.claim_generation(self.name) and snapshot.status(self.name).stopped

    def retirement(self, snapshot: RegistrySnapshot, original: Thread, before: int) -> OwnerCompletion:
        if not self.current(snapshot, original, before):
            raise RelationViolationError("Completed retirement differs from the selected original owner")
        self.completion.require_loss(snapshot, self)
        return self.completion

    def require_retired_for(self, snapshot: RegistrySnapshot, current: Thread) -> None:
        """A successor reads the release after the original left the registry: a retired
        session unregistered its process, so the released process is what retired."""
        self.completion.require_retired(snapshot, current, self.require_process())

    def require_owner_loss(self, snapshot, owner_identity):
        """Attest only this recorded owner's release and dead process.

        Input generation belongs to the recorded input, not to owner-loss proof.
        Neither this release nor missing input proves bytes were unwritten.
        """
        if not self.incarnation.current(snapshot):
            raise RelationViolationError("Released native owner incarnation changed")
        current = snapshot.require(self.name)
        owner_identity.require_recorded_owner(current)
        self.completion.require_loss(snapshot, self)
        actual = snapshot.claimed_identity(current.name)
        released = ClaimedIdentity(self.incarnation, self.after)
        if not actual.includes(released):
            raise RelationViolationError("Native release claim_scope regressed")
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
                # A stopped dead owner is already unregistered in `snapshot`: the release
                # names the process the original held, as record_session does.
                original.turn_record.read().require_idle()
                OwnerReleaseEvidence(original.name, original.incarnation.wire_birth,
                                     original.process_identity, before,
                                     snapshot.claim_generation(original.name)).upsert(db)

    def record_session(
        self, snapshot: RegistrySnapshot, original: Thread, before: int, owner: OwnerIdentity,
    ) -> None:
        original.turn_record.read().require_idle()
        with self.writing() as db:
            OwnerReleaseEvidence(
                original.name, original.incarnation.wire_birth, original.process_identity,
                before, snapshot.claim_generation(original.name), SessionRetiredOwnerCompletion(owner),
            ).upsert(db)

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        with self.writing() as db:
            for thread in threads:
                OwnerReleaseEvidence.delete(db, name=thread.name)


from .owner_cutover import OwnerCutover, PreserveOwnerRuntime


class OwnerLifecycle:
    def require_restartable(self, thread: Thread) -> None:
        """Do not interrupt a working turn; a wedged one ends the way stop ends it."""
        try:
            thread.require_restartable()
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
        thread = self.registry.require(name)
        thread.execution.require_native()
        thread.role.require_executable(
            RelationViolationError("A human participant cannot become an executor.")
        )
        if thread.process_alive:
            return thread
        with self.registry.store.editing() as edit:
            return self._register_owner(edit, thread, identity)

    def _register_owner(
        self, edit: RegistryEdit, previous: Thread, identity: ProcessIdentity,
        initial_input: LaunchInput = LaunchInput(),
    ) -> Thread:
        """Reserve one owner and its first input before releasing the child's start gate."""
        owner = self.registry.reserve_owner(edit, previous, identity)
        owned = owner.thread
        initial_input.record_first_input(partial(
            InputDispositions(self.root).record, owner=owned.name, target=owned.name,
            claim_scope=owner.claimed_generation,
        ))
        if previous.process_identity.offline():
            orders = tuple(
                todo
                for commander in CommanderService.boards(self.registry, self.goals)
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
        RestartNotice().publish(self.messaging, ThreadTarget(name),
                                f"Restarted holding {identities}; continue them from your branch")

    def ensure_owner(
        self, name: str, *, agent_bin: str = NativeArguments.default_binary,
        agent_args: LaunchArguments = InheritedArguments(),
    ) -> Thread:
        thread = self.registry.require(name)
        thread.execution.require_native()
        refusal = RelationViolationError("Explicit start is required for a stopped agent.")
        thread.role.require_executable(refusal)
        self.registry.status(thread.name).require_active(str(refusal))
        if thread.process_alive:
            return thread
        with self.launching_owner(
            thread, agent_bin, agent_args, launch_runtime=LaunchRuntime.running(),
        ) as owner:
            return owner

    def start_many(
        self, names: Sequence[str], *, agent_bin: LaunchBinary = ConfiguredBinary(),
        agent_args: LaunchArguments = InheritedArguments(),
    ) -> tuple[OwnerStartResult, ...]:
        """Dispatch all selected owners before joining their shared host's replies."""
        results = []
        with ExitStack() as launches:
            for name in names:
                thread = self.registry.require(name)
                thread.execution.require_native()
                status = self.registry.status(thread.name)
                refusal = RelationViolationError("Only visible agent threads can be started.")
                thread.role.require_executable(refusal)
                status.require_visible(str(refusal))
                if status.holds_process and thread.process_alive:
                    status.require_active("Stopped owner session has not retired yet.")
                    results.append(OwnerStartResult(thread.name, thread.require_process().pid, False))
                    continue
                owner = launches.enter_context(self.launching_owner(
                    thread, agent_bin.configured(), agent_args, launch_runtime=LaunchRuntime.running(),
                ))
                results.append(OwnerStartResult(owner.name, owner.require_process().pid, True))
        return tuple(results)

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

    def _stop_processes(self, owners: Sequence[tuple[Thread, int]]) -> None:
        """Join independent session retirements; never signal their shared host PID."""
        self._finish_stopped_owners(tuple(item for item in owners if not item[0].process_alive))
        requests = self.stop_requests(tuple(item for item in owners if item[0].process_alive))
        with ThreadPoolExecutor() as calls:
            tuple(calls.map(lambda request: OwnerConnection.exchange(
                self.root, self.registry.require(request.thread).require_process(), request), requests))
        self.retire_host()

    def stop_requests(self, owners: Sequence[tuple[Thread, int]]) -> tuple[StopOwnerRuntimeRequest, ...]:
        """Project the fenced originals into the one session retirement operation."""
        if not owners:
            return ()
        snapshot = self.registry.snapshot()
        requests = []
        for thread, generation in owners:
            snapshot.require_stopping_owner(thread, generation)
            requests.append(StopOwnerRuntimeRequest(
                thread=thread.name, owner=snapshot.owner_identity(thread.name),
                claim=ClaimedIdentity(thread.incarnation, generation),
            ))
        return tuple(requests)

    def fence_stop(self, *names: str) -> tuple[tuple[Thread, int], ...]:
        """Fence the complete selection in one edit, before any retirement joins."""
        with self.registry.store.editing() as edit:
            snapshot = edit.original.snapshot
            owners: list[tuple[Thread, int]] = []
            for name in dict.fromkeys(names):
                thread = edit.document.require(name)
                thread.role.require_executable(
                    RelationViolationError("Only agent threads can be stopped.")
                )
                if thread.process_identity.allows_owner_start():
                    if not snapshot.status(thread.name).stopped:
                        edit.document.unregister(thread.name)
                else:
                    owners.append((thread, snapshot.claim_generation(thread.name)))
            for thread, generation in owners:
                snapshot.require_stopping_owner(thread, generation)
                edit.document.fence_owner(thread.name)
            edit.commit()
            fenced = edit.original.snapshot
            return tuple((thread, fenced.claim_generation(thread.name)) for thread, _ in owners)

    def stop(self, *names: str) -> None:
        owners = self.fence_stop(*names)
        self._stop_processes(owners)
        # Completed per-session retirement, not the early fence, permits settlement.
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

    @contextmanager
    def launching_owner(
        self,
        thread: Thread,
        agent_bin: str,
        agent_args: LaunchArguments = InheritedArguments(),
        *,
        launch_runtime: LaunchRuntime,
        initial_input: LaunchInput = LaunchInput(),
        environment: Mapping[str, str] = INHERITED_ENVIRONMENT,
    ) -> Iterator[Thread]:
        # Retire the predecessor while its released declaration is still stopped.
        # Reserving the replacement PID first destroys that observation.
        RecoveryMonitorCapability.recover_owners(self, (thread,))
        env = dict(environment)
        launch_runtime.require_available()
        env = launch_runtime.environment(env)
        agent_bin = self.require_launch_authority().prepare_owner(thread, agent_bin, launch_runtime)
        env = initial_input.launch(thread.launch()).apply(env)
        env[NativeArguments.binary_key] = agent_bin
        agent_args.apply(env)
        thread.worktree.prepare()
        host = RootRuntimeStore.at(self.root).acquire_host(
            launch_runtime,
            (str(launch_runtime.interpreter), "-m", "agent_comms.worker",
             *LaunchRoot.from_path(self.launch_authority.child_root).arguments()),
            environment=launch_runtime.environment(ThreadLaunch.strip(dict(os.environ))),
            cwd=str(self.root),
            output=partial(OwnerProcessOutput.open, self.root, thread),
            room_for_start=partial(self.registry.storage_rule.room_for_start, thread, self.root),
        )
        with self.registry.store.editing() as edit:
            recorded = RootRuntimeStore.at(self.root).read().host.observed(self.root)
            if recorded != host or not recorded.alive():
                raise RelationViolationError("Worker host retired before owner registration")
            owned = self._register_owner(edit, thread, host, initial_input)
            owner = edit.original.snapshot.owner_identity(owned.name)
        with OwnerConnection.dispatching(self.root, host, StartOwnerRuntimeRequest(
            thread=owned.name, launch=RetainedOwnerLaunch(host, str(launch_runtime.interpreter), env),
            owner=owner,
        )) as response:
            try:
                yield owned
            finally:
                # Even a later launch failure must join every already-dispatched start.
                OwnerConnection.reply(response)

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

    def retire_host(self) -> None:
        """Retire the physical host only after every logical owner detached."""
        with self.registry.store.current_snapshot() as cut:
            RootRuntimeStore.at(self.root).retire_host(
                lambda process: not cut.snapshot.process_owners(process))

    def complete_session(
        self, owner: OwnerIdentity, expected: OwnerIdentity, process: ProcessIdentity, before: int,
    ) -> None:
        """Called only after the session's producers, turn and native child have joined."""
        with self.registry.store.editing() as edit:
            entry = edit.document.entry(expected.incarnation.name)
            if entry.owner_identity != expected or entry.thread.process_identity != process:
                raise RelationViolationError("Owner changed before completed session release")
            if owner.incarnation != expected.incarnation or owner.generation > expected.generation:
                raise RelationViolationError("Completed session names another owner lease")
            original = entry.thread
            original.turn_record.read().require_idle()
            edit.document.unregister(original.name)
            edit.commit()
            self.releases.record_session(edit.original.snapshot, original, before, owner)

    def release(self, name: str) -> None:
        """Let the calling participant mark itself stopped without signalling."""
        caller = ThreadLaunch.current().name
        with self.registry.store.editing() as edit:
            owner = edit.document.require(name)
            canonical = owner.name
            if edit.document.require(caller).name != canonical:
                raise RelationViolationError(f"Thread {caller!r} cannot release {canonical!r}.")
            owner.process_identity.require_release_by(ProcessIdentity.capture(os.getpid()))
            before = edit.original.snapshot.claim_generation(canonical)
            edit.document.fence_owner(canonical)
            edit.commit()
            self.releases.record(edit.original.snapshot, ((owner, before),))
