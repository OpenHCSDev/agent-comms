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
from typing import TYPE_CHECKING

from .child_process import DetachedProcess, ProcessIdentity
from .registration import Registration

if TYPE_CHECKING:
    pass
from .errors import RelationViolationError
from .locked_store import LockedStore
from .maintenance_barrier import MaintenanceBarrier
from .message_bus import MessageBus
from .registry_document import RegistrySnapshot
from .store_files import _store_lock
from .threads import Thread

_LOG = logging.getLogger(__name__)


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
        if self.before <= 0 or self.after <= self.before or self.thread.active_turn is not None:
            raise ValueError("Owner release requires an advanced admission and a closed turn")


class OwnerReleaseStore(LockedStore[dict[str, OwnerReleaseReceipt]]):
    @property
    def record_type(self):
        return dict[str, OwnerReleaseReceipt]

    def empty(self) -> dict[str, OwnerReleaseReceipt]:
        return {}


class OwnerLifecycle:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus):
        self.root = root
        self.registry = registry
        self.bus = bus
        self._wire_lock_path = root / "wire"
        self.maintenance = MaintenanceBarrier(registry.store.path)
        self.releases = OwnerReleaseStore(root / "owner_release_receipts.json")
        self._private_nk_launch: tuple[Path, str, Path] | None = None

    def pin_private_nk_launch(
        self, validated_root: Path, wire_root_id: str, native_package: Path
    ) -> None:
        """Bind an already preflighted ACP/worker launch to future owner handoffs.

        Public Comms instances never opt in from a marker or ambient env.
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
        self._private_nk_launch = (validated_root, wire_root_id, native_package)

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

    def restart_owners(
        self,
        names: Sequence[str] | None = None,
        *,
        agent_bin: str = "pi",
        agent_args: Sequence[str] | None = None,
        expected_incarnations: Mapping[str, tuple[int, float, int]] | None = None,
    ) -> tuple[OwnerRestartResult, ...]:
        """Preflight every idle owner before stopping any exact process identity."""
        with _store_lock(self._wire_lock_path):
            self.maintenance.assert_open_unlocked()
            snapshot = self.registry.snapshot()
            threads = (
                [
                    thread
                    for thread in snapshot.threads.values()
                    if thread.role.executable
                    and snapshot.statuses[thread.name].active
                    and thread.process_alive
                ]
                if names is None
                else list(
                    {
                        self.registry.require(name).name: self.registry.require(name)
                        for name in names
                    }.values()
                )
            )
            if expected_incarnations is not None and (
                len(threads) != 1 or set(expected_incarnations) != {t.name for t in threads}
            ):
                raise RelationViolationError("Guarded restart requires exactly one queued owner.")
            captured = []
            for thread in threads:
                generation = snapshot.admission_generations[thread.name]
                if expected_incarnations is not None and expected_incarnations[thread.name] != (
                    thread.pid,
                    thread.created_at,
                    generation,
                ):
                    raise RelationViolationError("Queued owner changed before restart.")
                if not thread.role.executable or not snapshot.statuses[thread.name].active:
                    raise RelationViolationError("Restart requires a running agent.")
                if not thread.process_alive or thread.pid == os.getpid():
                    raise RelationViolationError("Restart requires another live owner.")
                if thread.active_turn is not None:
                    raise RelationViolationError("Wait until the owner is idle before restart.")
                captured.append((thread, generation))
            # Fence before the first signal, so a concurrent channel wake cannot
            # start a turn while shutdown is pending. No replay is scheduled.
            captured = [
                (
                    thread,
                    self.registry.fence_idle_owner(
                        thread,
                        expected_admission_generation=generation,
                    ),
                )
                for thread, generation in captured
            ]
        for thread, generation in captured:
            self._stop_process(thread, generation)
        with _store_lock(self._wire_lock_path):
            for thread, generation in captured:
                self._require_same_stop_owner(thread, generation)
                if thread.process_alive:
                    raise RelationViolationError("Owner survived retirement.")
            return tuple(
                OwnerRestartResult(
                    thread.name,
                    thread.pid,
                    self._launch_owner_unlocked(
                        self.registry.require(thread.name),
                        agent_bin,
                        agent_args,
                    ).pid,
                )
                for thread, _ in captured
            )

    @contextmanager
    def _signal_guard(self, thread: Thread, admission_generation: int):
        with _store_lock(self._wire_lock_path):
            self._require_same_stop_owner(thread, admission_generation)
            yield

    def _stop_process(self, thread: Thread, admission_generation: int) -> None:
        assert thread.process_identity is not None
        with suppress(ProcessLookupError):
            DetachedProcess.attach(thread.process_identity).stop_sync(
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
        prompt: str | None = None,
    ) -> Thread:
        # Callers hold the wire lock; a phase change takes wire then registry.
        self.maintenance.assert_open_unlocked()
        env = os.environ.copy()
        for key in ("PI_PROMPT", "PI_PARENT_ID", "PI_TASK"):
            env.pop(key, None)
        private_launch = self._private_nk_launch
        if private_launch is not None:
            if agent_bin == "pi":
                # The default stock binary cannot attest native input IDs.
                # Keep the owner on this installation's pinned Pi entrypoint.
                agent_bin = str(Path(sys.executable).with_name("pi-comms-native"))
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
                # Preserve the preflight pin ONLY on explicit private launch.
                # Ordinary/public roots keep their canonical child path, even
                # when their original spelling was an absolute symlink.
                "AGENT_COMMS_ROOT": str(
                    private_launch[0] if private_launch is not None else self.root.resolve()
                ),
                "AGENT_COMMS_AGENT_BIN": agent_bin,
            }
        )
        if private_launch is not None:
            env["AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"] = private_launch[1]
            env["AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE"] = str(private_launch[2])
        if thread.parent is not None:
            env["PI_PARENT_ID"] = thread.parent
        if thread.task is not None:
            env["PI_TASK"] = thread.task
        if prompt is not None:
            env["PI_PROMPT"] = prompt
        if agent_args is not None:
            env["AGENT_COMMS_AGENT_ARGS"] = shlex.join(agent_args)
        owned: Thread | None = None

        def reserve(identity: ProcessIdentity) -> None:
            nonlocal owned
            owned = replace(thread, process_identity=identity, active_turn=None)
            self.registry.register(owned, new_owner=True)

        DetachedProcess.launch(
            (sys.executable, "-m", "agent_comms.worker"),
            env=env,
            cwd=thread.worktree,
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
        current = snapshot.threads.get(thread.name)
        if (
            current is None
            or (current.name, current.process_identity, current.created_at)
            != (thread.name, thread.process_identity, thread.created_at)
            or snapshot.admission_generations.get(thread.name) != admission_generation
        ):
            raise RelationViolationError(
                f"Owner changed while stopping {thread.name!r}; refusing a stale signal."
            )

    def _released_same_owner(
        self, snapshot: RegistrySnapshot, thread: Thread, admission_generation: int
    ) -> bool:
        current = snapshot.threads.get(thread.name)
        if (
            current is None
            or snapshot.admission_generations[thread.name] <= admission_generation
            or not snapshot.statuses[thread.name].stopped
            or current.active_turn is not None
            or (current.name, current.process_identity, current.created_at)
            != (thread.name, thread.process_identity, thread.created_at)
        ):
            return False
        return self.releases.read().get(thread.name) == OwnerReleaseReceipt(
            admission_generation,
            snapshot.admission_generations[thread.name],
            current,
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
        if owner.process_identity is not None and owner.process_identity != ProcessIdentity.capture(
            os.getpid()
        ):
            raise RelationViolationError("Only the registered owner may release itself.")
        before = snapshot.admission_generations[canonical]
        self.registry.unregister(canonical)
        after = self.registry.snapshot().admission_generations[canonical]
        receipt = OwnerReleaseReceipt(before, after, replace(owner, active_turn=None))
        self.releases.update(lambda receipts: {**receipts, canonical: receipt})
