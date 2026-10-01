"""Owner process lifecycle, maintenance and launch pin ownership."""

from __future__ import annotations

import logging
import os
import shlex
import sys
from collections.abc import Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from .child_process import ObservedProcess, ParentedProcess, ProcessIdentity
from .coordination_errors import PublicationActivationBlocked
from .diagnostics import owner_process_output
from .errors import RelationViolationError
from .locked_store import LockedStore
from .maintenance_barrier import MaintenanceBarrier
from .message_bus import MessageBus
from .owner_launch import RestartEnvironment
from .private_nk_entrypoint import PACKAGE_ENV, ROOT_ID_ENV, PrivateNkLaunch
from .registration import Registration
from .registry_document import RegistrySnapshot
from .store_files import _store_lock
from .threads import Thread
from .thread_identity import OwnerIdentity, AdmissionIdentity
from .restart_refusals import (
    OwnerSelectionChangedRefusal,
    OwnerGenerationChangedRefusal,
)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OwnerRestartSelection:
    identity: OwnerIdentity
    process: ProcessIdentity
    admission_generation: int

    @classmethod
    def capture(cls, snapshot: RegistrySnapshot, name: str):
        owner = snapshot.require_active(name)
        return cls(
            snapshot.owner_identity(owner.name),
            owner.require_process(),
            snapshot.admission_generations[owner.name],
        )

    @property
    def name(self) -> str:
        return self.identity.incarnation.name

    def require_current(self, snapshot: RegistrySnapshot) -> Thread:
        try:
            snapshot.require_owner_process(self.identity, self.process)
        except RelationViolationError as error:
            raise OwnerSelectionChangedRefusal() from error
        if snapshot.admission_generations[self.name] != self.admission_generation:
            raise OwnerGenerationChangedRefusal()
        return snapshot.require_active(self.name)


@dataclass(frozen=True, slots=True)
class OwnerRestartResult:
    thread: str
    previous_pid: int
    pid: int


@dataclass(frozen=True, slots=True)
class OwnerStartResult:
    thread: str
    pid: int
    launched: bool


@dataclass(frozen=True)
class OwnerReleaseReceipt:
    before: int
    after: int
    thread: Thread

    def __post_init__(self) -> None:
        if self.before <= 0 or self.after <= self.before:
            raise ValueError("Owner release requires an advanced admission and a closed turn")
        self.thread.require_idle()

    def current(self, snapshot: RegistrySnapshot, expected: Thread, admission: int) -> bool:
        """The stored release proves this retired owner, never a replacement."""
        if self.before != admission or self.thread.incarnation != expected.incarnation:
            return False
        if self.thread.process_identity != expected.process_identity:
            return False
        if self.after != snapshot.admission_generations.get(self.thread.name):
            return False
        if self.thread != snapshot.threads.get(self.thread.name):
            return False
        return snapshot.statuses[self.thread.name].stopped


    def require_native_loss(self, snapshot, source) -> None:
        """The original release attests only its exact dead process and admission.

        A recorded send can be fenced by a later release in the same incarnation.
        An unrecorded epoch instead needs the exact current stopped receipt. Both
        paths preserve UNKNOWN; neither says bytes were unwritten or permits retry.
        """
        current = snapshot.threads.get(self.thread.name)
        if current is None or current.incarnation != self.thread.incarnation:
            raise RelationViolationError("Released native owner incarnation changed")
        source.require_recorded_owner(current)
        process = self.thread.require_process()
        current.require_local_process(process)
        current.require_idle()
        if process.alive():
            raise RelationViolationError("Released native owner process is still alive")
        actual = snapshot.admission_identity(current.name)
        released = AdmissionIdentity(self.thread.incarnation, self.after)
        if not actual.includes(released):
            raise RelationViolationError("Native release admission regressed")
        source.sent_owner_admission_generation.require_release(self, snapshot, current)


class OwnerReleaseStore(LockedStore[dict[str, OwnerReleaseReceipt]]):
    @property
    def record_type(self):
        return dict[str, OwnerReleaseReceipt]

    def empty(self) -> dict[str, OwnerReleaseReceipt]:
        return {}


from .owner_cutover import OwnerCutover, PreserveOwnerRuntime


class OwnerLifecycle:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus):
        self.root = root
        self.registry = registry
        self.bus = bus
        self._wire_lock_path = root / "wire"
        self.maintenance = MaintenanceBarrier(registry.store.path)
        self.releases = OwnerReleaseStore(root / "owner_release_receipts.json")
        self._private_nk_launch: PrivateNkLaunch | None = None

    def pin_private_nk_launch(
        self, validated_root: Path, wire_root_id: str, native_package: Path
    ) -> None:
        """Bind an already preflighted ACP/worker launch to future owner handoffs.

        A marker never grants authority. Service factories validate explicit
        environment pairs; direct composition requires a caller-supplied pin.
        Recheck the root marker before retaining the exact lexical absolute
        path; child env is generated from this pin, not a later cwd/env read.
        """
        if (
            not isinstance(validated_root, Path)
            or not validated_root.is_absolute()
            or validated_root != self.root
            or not isinstance(native_package, Path)
            or not native_package.is_absolute()
        ):
            raise ValueError("private owner launch requires the validated absolute root/package")
        with self.bus.log.locked():
            marker = self.bus.log._private_marker_unlocked()
            if marker.root_id != wire_root_id:
                raise RelationViolationError("private owner launch root ID changed")
        self._private_nk_launch = PrivateNkLaunch(
            validated_root, wire_root_id, native_package, None
        )

    def acquire_thread(self, name: str, *, owner_pid: int) -> Thread:
        """Bind only this calling process; stored birth time is signal authority."""
        if owner_pid != os.getpid():
            raise RelationViolationError("An owner may acquire only its own process identity.")
        identity = ProcessIdentity.capture(owner_pid)
        with _store_lock(self._wire_lock_path):
            self.maintenance.assert_open_unlocked()
            thread = self.registry.require(name)
            if not thread.role.executable:
                raise RelationViolationError("A human participant cannot become an executor.")
            if thread.process_alive:
                return thread
            owned = replace(thread, process_identity=identity, active_turn=None)
            self.registry.register(owned, new_owner=True)
            return owned

    def ensure_owner(
        self, name: str, *, agent_bin: str = "pi", agent_args: Sequence[str] | None = None
    ) -> Thread:
        with _store_lock(self._wire_lock_path):
            self.maintenance.assert_open_unlocked()
            thread = self.registry.require(name)
            if not thread.role.executable or not self.registry.status(thread.name).active:
                raise RelationViolationError("Explicit start is required for a stopped agent.")
            return (
                thread
                if thread.process_alive
                else self._launch_owner_unlocked(thread, agent_bin, agent_args)
            )

    def start(
        self, name: str, *, agent_bin: str | None = None, agent_args: Sequence[str] | None = None
    ) -> OwnerStartResult:
        with _store_lock(self._wire_lock_path):
            self.maintenance.assert_open_unlocked()
            thread = self.registry.require(name)
            status = self.registry.status(thread.name)
            if not thread.role.executable or not status.visible:
                raise RelationViolationError("Only visible agent threads can be started.")
            if thread.process_alive:
                if not status.active:
                    raise RelationViolationError("Stopped owner has not exited yet.")
                return OwnerStartResult(thread.name, thread.pid, False)
            owner = self._launch_owner_unlocked(
                thread, agent_bin or os.environ.get("AGENT_COMMS_AGENT_BIN", "pi"), agent_args
            )
            return OwnerStartResult(owner.name, owner.pid, True)

    def restart_entrypoint(self, source_binary: str) -> str:
        """Choose this runtime's reviewed native entrypoint for a private handoff."""
        if self._private_nk_launch is None:
            return source_binary
        self._private_nk_launch.validate()
        return self.native_entrypoint()

    @staticmethod
    def native_entrypoint() -> str:
        return str(Path(sys.executable).with_name("pi-comms-native"))

    def restart_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        """Project the retained launch authority into the target runtime policy."""
        result = dict(environment)
        if self._private_nk_launch is not None:
            self._private_nk_launch.validate()
            self._private_nk_launch.apply_environment(result)
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

        The declared cutover runs under the wire admission lock after every
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
    def _signal_guard(self, thread: Thread, admission_generation: int):
        with _store_lock(self._wire_lock_path):
            self._require_same_stop_owner(thread, admission_generation)
            yield

    def _stop_process(self, thread: Thread, admission_generation: int) -> None:
        identity = thread.require_process()
        with suppress(ProcessLookupError):
            ObservedProcess(identity).stop_sync(
                guard=lambda: self._signal_guard(thread, admission_generation),
            )

    def stop(self, name: str) -> None:
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            thread = self.registry.require(name)
            if not snapshot.statuses[thread.name].active:
                return
            if thread.pid == os.getpid():
                caller = os.environ.get("PI_AGENT_ID") or os.environ.get("AGENT_COMMS_THREAD")
                if caller and self.registry.require(caller).name == thread.name:
                    self._release_current_owner_unlocked(thread.name)
                else:
                    self.registry.unregister(thread.name)
                return
            if not thread.process_alive:
                self.registry.unregister(thread.name)
                return
            generation = snapshot.admission_generations[thread.name]
        self._stop_process(thread, generation)
        self._finish_stopped_owner(thread, generation)

    def _launch_owner_unlocked(
        self,
        thread: Thread,
        agent_bin: str,
        agent_args: Sequence[str] | None = None,
        *,
        startup_input_key: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> Thread:
        # Callers hold the wire lock; a phase change takes wire then registry.
        self.maintenance.assert_open_unlocked()
        env = dict(os.environ if environment is None else environment)
        for key in (
            "PI_PROMPT",
            "PI_PARENT_ID",
            "PI_TASK",
            "AGENT_COMMS_STARTUP_INPUT_KEY",
            ROOT_ID_ENV,
            PACKAGE_ENV,
        ):
            env.pop(key, None)
        private_launch = self._private_nk_launch
        if private_launch is None:
            with self.bus.log.locked():
                if self.bus.log.read_metadata_unlocked().private:
                    raise PublicationActivationBlocked(
                        "private owner launch requires explicit matching root and package"
                    )
        else:
            private_launch.validate()
        if private_launch is not None:
            if agent_bin == "pi":
                # The default stock binary cannot attest native input IDs.
                # Keep the owner on this installation's pinned Pi entrypoint.
                agent_bin = self.native_entrypoint()
            # Explicit launch installs the current runtime before registering
            # this owner. Readers never create or repair runtime schemas.
            from .bus_publication import stable_thread_lookup
            from .coordinator import Coordination

            with Coordination(str(self.root / "coordination.sqlite3")) as store:
                store.install_private_runtime()
                store.participants.register(
                    stable_thread_lookup(thread.created_at),
                    thread.name,
                    thread.name,
                    committed=True,
                )
        env.update(
            {
                "PI_AGENT_ID": thread.name,
                "AGENT_COMMS_THREAD": thread.name,
                "PI_AGENT_TAGS": ",".join(sorted(thread.tags)),
                "AGENT_COMMS_TAGS": ",".join(sorted(thread.tags)),
                "PI_WORKTREE": thread.worktree,
                "AGENT_COMMS_AGENT_BIN": agent_bin,
            }
        )
        if private_launch is not None:
            private_launch.apply_environment(env)
        else:
            # Ordinary roots keep their canonical child path across symlink changes.
            env["AGENT_COMMS_ROOT"] = str(self.root.resolve())
        if thread.parent is not None:
            env["PI_PARENT_ID"] = thread.parent
        if thread.task is not None:
            env["PI_TASK"] = thread.task
        if startup_input_key is not None:
            env["AGENT_COMMS_STARTUP_INPUT_KEY"] = startup_input_key
        if agent_args is not None:
            env["AGENT_COMMS_AGENT_ARGS"] = shlex.join(agent_args)
        owned: Thread | None = None

        def reserve(identity: ProcessIdentity) -> None:
            nonlocal owned
            owned = replace(thread, process_identity=identity, active_turn=None)
            self.registry.register(owned, new_owner=True)

        with owner_process_output(self.root, thread) as output:
            ParentedProcess.launch(
                (sys.executable, "-m", "agent_comms.worker"),
                env=env,
                cwd=thread.worktree,
                output=output,
                before_start=reserve,
            )
        assert owned is not None
        return owned

    def _require_same_stop_owner(self, thread: Thread, admission_generation: int) -> None:
        snapshot = self.registry.snapshot()
        # Voluntary release advances admission and marks STOPPED before Python
        # finishes shutting down. Its exact receipt preserves the signaled
        # incarnation; it does not prove OS exit or authorize a replacement PID.
        if self._released_same_owner(snapshot, thread, admission_generation):
            return
        try:
            snapshot.require_stopping_owner(thread, admission_generation)
        except RelationViolationError as error:
            raise RelationViolationError(
                f"Owner changed while stopping {thread.name!r}; refusing a stale signal."
            ) from error

    def _released_same_owner(
        self, snapshot: RegistrySnapshot, thread: Thread, admission_generation: int
    ) -> bool:
        receipt = self.releases.read().get(thread.name)
        return (
            receipt.current(snapshot, thread, admission_generation)
            if receipt is not None
            else False
        )

    def _finish_stopped_owner(self, thread: Thread, admission_generation: int) -> None:
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            if self._released_same_owner(snapshot, thread, admission_generation):
                return  # An exact, durably attested release of the signaled owner.
            self._require_same_stop_owner(thread, admission_generation)
            self.registry.unregister(thread.name)

    def release(self, name: str) -> None:
        """Let the calling participant mark itself stopped without signalling."""
        caller = os.environ.get("PI_AGENT_ID") or os.environ.get("AGENT_COMMS_THREAD")
        if not caller:
            raise RelationViolationError(
                "Voluntary release requires PI_AGENT_ID or AGENT_COMMS_THREAD."
            )
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.require(name).name
            if self.registry.require(caller).name != canonical:
                raise RelationViolationError(f"Thread {caller!r} cannot release {canonical!r}.")
            self._release_current_owner_unlocked(canonical)

    def _release_current_owner_unlocked(self, canonical: str) -> None:
        snapshot = self.registry.snapshot()
        owner = snapshot.threads[canonical]
        if (
            owner.process_identity is not None
            and owner.process_identity != ProcessIdentity.capture(os.getpid())
        ):
            raise RelationViolationError("Only the registered owner may release itself.")
        before = snapshot.admission_generations[canonical]
        self.registry.unregister(canonical)
        after = self.registry.snapshot().admission_generations[canonical]
        receipt = OwnerReleaseReceipt(before, after, replace(owner, active_turn=None))
        self.releases.update(lambda receipts: {**receipts, canonical: receipt})
