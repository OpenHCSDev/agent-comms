"""Owner process lifecycle, maintenance and launch pin ownership."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import select
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .field_codec import FieldCodec
from .registration import Registration
from .thread_identity import OwnerIdentity

if TYPE_CHECKING:
    pass
from .errors import RelationViolationError
from .maintenance_barrier import MaintenanceBarrier
from .message_bus import MessageBus
from .registry_document import RegistrySnapshot
from .store_files import _atomic_write_text, _store_lock
from .threads import Thread

_LOG = logging.getLogger(__name__)


def _owner_launch_proof(owner: OwnerIdentity, pid: int) -> bytes:
    """Fixed-size pipe proof bound to the complete name and owner incarnation.

    A valid thread name has no protocol length limit. Passing its plaintext
    under the wire lock could fill the pipe before the child can acquire it.
    """
    identity = json.dumps((FieldCodec.encode(owner), pid), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).digest()


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


OBSERVATION_INTERVAL = 0.05


class OwnerLifecycle:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus):
        self.root = root
        self.registry = registry
        self.bus = bus
        self._wire_lock_path = root / "wire"
        self.maintenance = MaintenanceBarrier(registry.store.path)
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
            if marker["wire_root_id"] != wire_root_id:
                raise RelationViolationError("private owner launch root ID changed")
        self._private_nk_launch = (validated_root, wire_root_id, native_package)

    def acquire_thread(self, name: str, *, owner_pid: int) -> Thread:
        """Claim an offline thread, or return its existing live owner unchanged."""
        from dataclasses import replace

        with _store_lock(self._wire_lock_path):
            self.maintenance.assert_open_unlocked()
            thread = self.registry.require(name)
            if not thread.role.executable:
                raise RelationViolationError("A human participant cannot become an agent executor.")
            if thread.pid > 0 and thread.pid != owner_pid and self._process_alive(thread.pid):
                return thread
            reservation = os.environ.pop("AGENT_COMMS_RESERVATION_FD", None)
            if reservation is not None:
                try:
                    fd = int(reservation)
                    ready, _, _ = select.select([fd], [], [], 5.0)
                    evidence = os.read(fd, hashlib.sha256().digest_size) if ready else b""
                except (OSError, ValueError) as error:
                    raise RelationViolationError("Invalid owner startup reservation.") from error
                finally:
                    with suppress(OSError, ValueError):
                        os.close(int(reservation))
                snapshot = self.registry.snapshot()
                if thread.name not in snapshot.owner_generations:
                    raise RelationViolationError("Owner startup reservation has no incarnation.")
                expected = _owner_launch_proof(snapshot.owner_identity(thread.name), owner_pid)
                if (
                    evidence != expected
                    or thread.pid != owner_pid
                    or thread.active_turn is not None
                    or not self.registry.status(thread.name).active
                ):
                    raise RelationViolationError("Owner startup reservation no longer matches.")
                # An inherited pipe from our own launcher proves this exact
                # registry reservation. A coincidentally reused PID cannot.
                return thread
            owned = replace(thread, pid=owner_pid, active_turn=None)
            self.registry.register(owned, new_owner=True)
            return owned

    def ensure_owner(
        self, name: str, *, agent_bin: str = "pi", agent_args: Sequence[str] | None = None
    ) -> Thread:
        """Attach or launch an active thread; never revive an intentionally stopped one."""
        with _store_lock(self._wire_lock_path):
            self.maintenance.assert_open_unlocked()
            thread = self.registry.require(name)
            if not thread.role.executable:
                raise RelationViolationError("A human participant cannot become an agent executor.")
            if not self.registry.status(thread.name).active:
                raise RelationViolationError(
                    f"Thread {thread.name!r} is stopped or archived; use explicit comms_start."
                )
            if thread.pid > 0 and self._process_alive(thread.pid):
                return thread
            return self._launch_owner_unlocked(thread, agent_bin, agent_args)

    def start(
        self, name: str, *, agent_bin: str | None = None, agent_args: Sequence[str] | None = None
    ) -> OwnerStartResult:
        """Explicitly resume a visible agent thread, reserving at most one owner.

        Starting an already-live thread is idempotent. This does not submit a
        prompt or interrupt a turn; the detached owner resumes its saved state.
        The PID receipt is a reservation, not a completed startup handshake.
        """
        original_owner: tuple[str, int, float] | None = None
        original_generation: int | None = None
        for _ in range(3):
            with _store_lock(self._wire_lock_path):
                self.maintenance.assert_open_unlocked()
                snapshot = self.registry.snapshot()
                canonical = snapshot.aliases.get(name, name)
                thread = self.registry.require(name)
                if not thread.role.executable or not snapshot.statuses[canonical].visible:
                    raise RelationViolationError("Only visible agent threads can be started.")
                identity = (canonical, thread.pid, thread.created_at)
                if original_owner is not None and identity != original_owner:
                    raise RelationViolationError(f"Owner changed while starting {name!r}.")
                original_owner = identity
                admission_generation = snapshot.admission_generations.get(canonical)
                if original_generation is not None and admission_generation != original_generation:
                    raise RelationViolationError(f"Owner epoch changed while starting {name!r}.")
                if thread.pid <= 0 or not self._process_alive(thread.pid):
                    owner = self._launch_owner_unlocked(
                        thread,
                        agent_bin or os.environ.get("AGENT_COMMS_AGENT_BIN", "pi"),
                        agent_args,
                    )
                    return OwnerStartResult(owner.name, owner.pid, True)
                if not snapshot.statuses[canonical].active:
                    raise RelationViolationError(
                        "Cannot reactivate a stopped incarnation before its owner exits."
                    )
                if admission_generation is None:
                    raise RelationViolationError("Cannot start an owner without an incarnation.")
                original_generation = admission_generation

            # A just-forked worker needs the wire lock to create its socket.
            if not self._is_local_participant(thread):
                raise RelationViolationError(
                    f"Cannot reuse unverifiable process {thread.pid} for {thread.name!r}."
                )
            with _store_lock(self._wire_lock_path):
                self.maintenance.assert_open_unlocked()
                current = self.registry.snapshot()
                fresh = current.threads.get(canonical)
                if (
                    fresh is None
                    or (canonical, fresh.pid, fresh.created_at) != original_owner
                    or not current.statuses[canonical].visible
                ):
                    raise RelationViolationError(f"Owner changed while starting {name!r}.")
                if current.admission_generations.get(canonical) != original_generation:
                    raise RelationViolationError(f"Owner epoch changed while starting {name!r}.")
                if not self._process_alive(fresh.pid) or not self._is_local_participant(
                    fresh, wait=False
                ):
                    continue
                if not current.statuses[canonical].active:
                    raise RelationViolationError(
                        "Cannot reactivate a stopped incarnation before its owner exits."
                    )
                return OwnerStartResult(canonical, fresh.pid, False)
        raise RelationViolationError(
            f"Owner changed or became unverifiable while starting {name!r}."
        )

    def restart_owners(
        self,
        names: Sequence[str] | None = None,
        *,
        agent_bin: str = "pi",
        agent_args: Sequence[str] | None = None,
        expected_incarnations: Mapping[str, tuple[int, float, int]] | None = None,
    ) -> tuple[OwnerRestartResult, ...]:
        """Preflight all owners together; release the wire lock for proof and exit.

        No owner is signaled until every selected owner has been verified again
        under the wire lock. The OS may still fail partway through signaling;
        that uncertainty is reported rather than claiming an atomic restart.
        """
        selection: tuple[tuple[str, int, float], ...] | None = None
        original_generations: tuple[int, ...] | None = None
        for _ in range(3):
            with _store_lock(self._wire_lock_path):
                self.maintenance.assert_open_unlocked()
                snapshot = self.registry.snapshot()
                if names is None:
                    threads = [
                        thread
                        for thread in snapshot.threads.values()
                        if thread.role.executable
                        and snapshot.statuses[thread.name].active
                        and thread.pid > 0
                        and self._process_alive(thread.pid)
                    ]
                else:
                    threads = list(
                        {
                            snapshot.aliases.get(name, name): self.registry.require(name)
                            for name in names
                        }.values()
                    )
                identities = tuple(
                    (thread.name, thread.pid, thread.created_at) for thread in threads
                )
                if selection is not None and identities != selection:
                    raise RelationViolationError("Owner selection changed before restart.")
                selection = identities
                if expected_incarnations is not None and (
                    len(threads) != 1
                    or set(expected_incarnations) != {thread.name for thread in threads}
                ):
                    raise RelationViolationError(
                        "Guarded restart requires exactly one queued owner."
                    )
                captured = []
                for thread in threads:
                    admission_generation = snapshot.admission_generations.get(thread.name)
                    if expected_incarnations is not None and expected_incarnations[thread.name] != (
                        thread.pid,
                        thread.created_at,
                        admission_generation,
                    ):
                        raise RelationViolationError(
                            "Queued owner incarnation changed before restart."
                        )
                    if (
                        not thread.role.executable
                        or not snapshot.statuses[thread.name].active
                        or thread.pid <= 0
                        or not self._process_alive(thread.pid)
                    ):
                        raise ValueError(f"Thread {thread.name!r} has no running owner to restart.")
                    if thread.pid == os.getpid():
                        raise ValueError("An owner cannot restart itself; use the external CLI.")
                    if thread.active_turn is not None:
                        raise ValueError(
                            f"Thread {thread.name!r} has an active turn; wait until idle."
                        )
                    if admission_generation is None:
                        raise RelationViolationError(
                            "Cannot restart an owner without an incarnation."
                        )
                    captured.append((thread, admission_generation))
                generations = tuple(
                    admission_generation for _thread, admission_generation in captured
                )
                if original_generations is not None and generations != original_generations:
                    raise RelationViolationError("Owner epochs changed before restart.")
                original_generations = generations

            # A newly forked owner's socket may depend on this same wire lock.
            for thread, _generation in captured:
                if not self._is_local_participant(thread):
                    raise RelationViolationError(
                        f"Refusing to restart unverifiable process {thread.pid} "
                        f"for {thread.name!r}."
                    )
            with _store_lock(self._wire_lock_path):
                self.maintenance.assert_open_unlocked()
                fresh = self.registry.snapshot()
                if any(
                    (
                        fresh.threads.get(thread.name) is None
                        or (
                            thread.name,
                            fresh.threads[thread.name].pid,
                            fresh.threads[thread.name].created_at,
                        )
                        != (thread.name, thread.pid, thread.created_at)
                        or not fresh.statuses[thread.name].active
                    )
                    for thread, _generation in captured
                ):
                    raise RelationViolationError("Owner selection changed before restart.")
                if any(
                    fresh.admission_generations.get(thread.name) != admission_generation
                    for thread, admission_generation in captured
                ):
                    raise RelationViolationError("Owner epochs changed before restart.")
                if any(
                    fresh.threads[thread.name].active_turn is not None
                    for thread, _generation in captured
                ):
                    raise RelationViolationError("Owner became busy before restart.")
                if any(
                    not self._process_alive(thread.pid)
                    or not self._is_local_participant(thread, wait=False)
                    for thread, _generation in captured
                ):
                    continue
                if expected_incarnations is not None:
                    # Persist the admission fence BEFORE signaling. An owner
                    # receiving a wake after this lock releases cannot claim
                    # a turn while SIGTERM is pending: lease_local_turn rejects
                    # STOPPED. Never undo this fence on an uncertain signal.
                    stop_generations = {
                        thread.name: self.registry.fence_idle_owner(
                            thread, expected_admission_generation=admission_generation
                        )
                        for thread, admission_generation in captured
                    }
                for thread, _generation in captured:
                    with suppress(ProcessLookupError):
                        self._signal_local_owner(thread.pid, signal.SIGTERM)
                break
        else:
            raise RelationViolationError("Owner selection changed or became unverifiable.")

        alive = [
            (thread, admission_generation)
            for thread, admission_generation in captured
            if not self._wait_for_owner_exit(thread.pid, 3.0)
        ]
        if alive:
            with _store_lock(self._wire_lock_path):
                signal_targets = []
                for thread, admission_generation in alive:
                    if expected_incarnations is not None:
                        stop_snapshot = self.registry.snapshot()
                        existing = stop_snapshot.threads.get(thread.name)
                        if (
                            existing is None
                            or (existing.pid, existing.created_at)
                            != (thread.pid, thread.created_at)
                            or not stop_snapshot.statuses[thread.name].stopped
                            or stop_snapshot.admission_generations.get(thread.name)
                            != stop_generations[thread.name]
                        ) and not self._released_same_owner(
                            stop_snapshot, thread, stop_generations[thread.name]
                        ):
                            raise RelationViolationError("Fenced owner changed after signal.")
                    else:
                        self._require_same_stop_owner(thread, admission_generation)
                    if self._wait_for_owner_exit(thread.pid, 0):
                        continue
                    if not self._is_local_participant(thread, wait=False):
                        if self._wait_for_owner_exit(thread.pid, 0):
                            continue
                        raise RelationViolationError(
                            f"Refusing to signal unverifiable process {thread.pid}."
                        )
                    signal_targets.append(thread)
                for thread in signal_targets:
                    with suppress(ProcessLookupError):
                        self._signal_local_owner(thread.pid, signal.SIGKILL)
            remaining = [
                thread.pid
                for thread, _generation in alive
                if not self._wait_for_owner_exit(thread.pid, 1.0)
            ]
            if remaining:
                diagnostics = "; ".join(self._stop_failure_probe(pid) for pid in remaining)
                raise RuntimeError(f"Owner processes did not stop: {diagnostics}")

        with _store_lock(self._wire_lock_path):
            final = self.registry.snapshot()
            for thread, admission_generation in captured:
                final_owner = final.threads.get(thread.name)
                if expected_incarnations is not None:
                    if (
                        final_owner is None
                        or (final_owner.pid, final_owner.created_at)
                        != (thread.pid, thread.created_at)
                        or not final.statuses[thread.name].stopped
                        or final.admission_generations.get(thread.name)
                        != stop_generations[thread.name]
                    ) and not self._released_same_owner(
                        final, thread, stop_generations[thread.name]
                    ):
                        raise RelationViolationError(
                            "Fenced owner changed after signal."
                        )
                    if self._process_alive(thread.pid):
                        raise RelationViolationError("Fenced owner survived after signal.")
                elif not self._released_same_owner(final, thread, admission_generation):
                    self._require_same_stop_owner(thread, admission_generation)
            if expected_incarnations is None:
                for thread, _generation in captured:
                    if self.registry.status(thread.name).active:
                        self.registry.unregister(thread.name)
            results = []
            for thread, _generation in captured:
                ready_owner = self.registry.require(thread.name)
                owner = self._launch_owner_unlocked(ready_owner, agent_bin, agent_args)
                results.append(OwnerRestartResult(thread.name, thread.pid, owner.pid))
            return tuple(results)

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
        for key in ("PI_PROMPT", "PI_PARENT_ID", "PI_TASK", "AGENT_COMMS_RESERVATION_FD"):
            env.pop(key, None)
        private_launch = self._private_nk_launch
        if private_launch is not None:
            if agent_bin == "pi":
                # The default stock binary cannot attest native input IDs.
                # Keep the owner on this installation's pinned Pi entrypoint.
                agent_bin = str(Path(sys.executable).with_name("pi-comms-native"))
            # Cutover stages its owners up front, but a later owner can be
            # registered on the active route. The cohort reader needs this
            # same immutable creation identity before the worker can wake.
            from .bus_publication import stable_thread_lookup
            from .coordination_store import MutationStore

            with MutationStore(str(self.root / "coordination.sqlite3")) as store:
                store.register_participant(
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
        # The read end is inherited only by this worker. Send its committed
        # epoch AFTER registration: a crash before that point fails startup
        # closed instead of making a stale same-PID record authoritative.
        read_fd, write_fd = os.pipe() if os.name == "posix" else (-1, -1)
        if read_fd >= 0:
            env["AGENT_COMMS_RESERVATION_FD"] = str(read_fd)
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "agent_comms.worker"],
                env=env,
                cwd=thread.worktree,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(read_fd,) if read_fd >= 0 else (),
            )
        except BaseException:
            if write_fd >= 0:
                os.close(write_fd)
            raise
        finally:
            if read_fd >= 0:
                os.close(read_fd)
        owned = replace(thread, pid=process.pid, active_turn=None)
        try:
            self.registry.register(owned, new_owner=True)
            if write_fd >= 0:
                owner_identity = self.registry.snapshot().owner_identity(thread.name)
                proof = _owner_launch_proof(owner_identity, process.pid)
                if os.write(write_fd, proof) != len(proof):
                    raise RelationViolationError("Owner startup reservation was not fully sent.")
        except BaseException:
            process.terminate()
            process.wait()
            raise
        finally:
            if write_fd >= 0:
                os.close(write_fd)
        return owned

    def stop(self, name: str) -> None:
        """Stop one verified owner without holding the wire lock through startup or exit.

        On Darwin the worker must acquire this lock before it can create the
        kernel-authenticated owner socket. Waiting for that socket under the
        lock would prevent a newly forked worker from ever proving its PID.
        """
        original_owner: tuple[str, int, float] | None = None
        original_generation: int | None = None
        for attempt in range(3):
            with _store_lock(self._wire_lock_path):
                snapshot = self.registry.snapshot()
                canonical = snapshot.aliases.get(name, name)
                thread = snapshot.threads.get(canonical)
                if thread is None:
                    self.registry.require(name)  # Preserve the ordinary unknown-name error.
                    raise AssertionError("Registered thread disappeared from its snapshot")
                identity = (canonical, thread.pid, thread.created_at)
                if original_owner is not None and identity != original_owner:
                    raise RelationViolationError(
                        f"Owner changed while stopping {name!r}; refusing a stale signal."
                    )
                original_owner = identity
                admission_generation = snapshot.admission_generations.get(canonical)
                if original_generation is not None and admission_generation != original_generation:
                    raise RelationViolationError(f"Owner epoch changed while stopping {name!r}.")
                if not snapshot.statuses[canonical].active:
                    if attempt == 0:
                        return
                    raise RelationViolationError(f"Owner changed while stopping {name!r}.")
                if thread.pid == os.getpid():
                    caller = os.environ.get("PI_AGENT_ID") or os.environ.get("AGENT_COMMS_THREAD")
                    if caller and self.registry.require(caller).name == canonical:
                        self._release_current_owner_unlocked(canonical)
                    else:
                        self.registry.unregister(canonical)
                    return
                if thread.pid <= 0 or not self._process_alive(thread.pid):
                    self.registry.unregister(canonical)
                    return
                if admission_generation is None:
                    raise RelationViolationError("Cannot stop an owner without an incarnation.")
                original_generation = admission_generation

            # Do not wait while holding the wire lock: startup and graceful
            # shutdown both need it. Recheck the exact incarnation before any
            # signal, so a replaced owner cannot inherit an old stop request.
            if not self._is_local_participant(thread):
                raise RelationViolationError(
                    f"Refusing to signal unverifiable process {thread.pid} for {name!r}."
                )
            with _store_lock(self._wire_lock_path):
                current = self.registry.snapshot()
                current_thread = current.threads.get(canonical)
                if (
                    current_thread is None
                    or (canonical, current_thread.pid, current_thread.created_at) != original_owner
                    or not current.statuses[canonical].active
                ):
                    raise RelationViolationError(
                        f"Owner changed while stopping {name!r}; refusing a stale signal."
                    )
                if current.admission_generations.get(canonical) != original_generation:
                    raise RelationViolationError(f"Owner epoch changed while stopping {name!r}.")
                if not self._process_alive(thread.pid):
                    self.registry.unregister(canonical)
                    return
                # Nonblocking fresh kernel proof immediately before signaling.
                if not self._is_local_participant(thread, wait=False):
                    continue
                try:
                    self._signal_local_owner(thread.pid, signal.SIGTERM)
                except ProcessLookupError:
                    self.registry.unregister(canonical)
                    return
                break
        else:
            raise RelationViolationError(
                f"Owner changed or became unverifiable while stopping {name!r}."
            )

        if self._wait_for_owner_exit(thread.pid, 3.0):
            self._finish_stopped_owner(thread, admission_generation)
            return
        with _store_lock(self._wire_lock_path):
            self._require_same_stop_owner(thread, admission_generation)
            if not self._wait_for_owner_exit(thread.pid, 0):
                if not self._is_local_participant(thread, wait=False):
                    if not self._wait_for_owner_exit(thread.pid, 0):
                        raise RelationViolationError(
                            f"Refusing to signal unverifiable process {thread.pid} for {name!r}."
                        )
                else:
                    with suppress(ProcessLookupError):
                        self._signal_local_owner(thread.pid, signal.SIGKILL)
        if not self._wait_for_owner_exit(thread.pid, 1.0):
            raise RuntimeError(f"Process did not stop: {self._stop_failure_probe(thread.pid)}")
        self._finish_stopped_owner(thread, admission_generation)

    @staticmethod
    def _stop_failure_probe(pid: int) -> str:
        """Bounded state-only Mac CI diagnostic, never a death or ownership proof."""
        if sys.platform != "darwin":
            return f"pid={pid}"
        try:
            probe = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "stat="],
                capture_output=True,
                text=True,
                check=False,
                timeout=1,
            )
            state = f"ps_rc={probe.returncode}, ps_stat={probe.stdout.strip()[:24]!r}"
        except (OSError, subprocess.TimeoutExpired) as error:
            state = f"ps_error={type(error).__name__}"
        # In a failing stop only, find whether this PID is our exited child.
        # waitpid may reap it; the result is diagnostic, not false success.
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
            child = f"waitpid={reaped}"
        except (ChildProcessError, OSError) as error:
            child = f"waitpid_error={type(error).__name__}"
        return f"pid={pid}, {state}, {child}"

    @staticmethod
    def _signal_local_owner(pid: int, signum: signal.Signals) -> None:
        if os.name == "posix" and os.getpgid(pid) == pid:
            os.killpg(pid, signum)
        else:
            os.kill(pid, signum)

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
            or (current.name, current.pid, current.created_at)
            != (thread.name, thread.pid, thread.created_at)
            or snapshot.admission_generations.get(thread.name) != admission_generation
            or not snapshot.statuses[thread.name].active
        ):
            raise RelationViolationError(
                f"Owner changed while stopping {thread.name!r}; refusing a stale signal."
            )

    def _wait_for_owner_exit(self, pid: int, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            # Observe a direct child's exit WITHOUT reaping it behind its
            # Popen/parent's back. An unrelated CLI process has no waitid
            # authority and falls back to ps/proc.
            if os.name == "posix" and hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
                try:
                    result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if result is not None and result.si_pid == pid:
                        return True
                except (ChildProcessError, OSError):
                    pass
            if not self._process_alive(pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _read_owner_release_receipts(self) -> dict[str, dict[str, object]]:
        try:
            raw = json.loads((self.root / "owner_release_receipts.json").read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise RelationViolationError("Owner release receipt is unreadable.") from error
        if not isinstance(raw, dict) or any(not isinstance(value, dict) for value in raw.values()):
            raise RelationViolationError("Owner release receipt is invalid.")
        return raw

    def _released_same_owner(
        self, snapshot: RegistrySnapshot, thread: Thread, admission_generation: int
    ) -> bool:
        current = snapshot.threads.get(thread.name)
        if (
            current is None
            or not snapshot.statuses[thread.name].stopped
            or current.active_turn is not None
            or (current.name, current.pid, current.created_at)
            != (thread.name, thread.pid, thread.created_at)
        ):
            return False
        return self._read_owner_release_receipts().get(thread.name) == {
            "pid": thread.pid,
            "before": admission_generation,
            "after": snapshot.admission_generations.get(thread.name),
            "thread": json.dumps(current.to_wire(), sort_keys=True),
        }

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
        if owner.pid > 0 and owner.pid != os.getpid():
            raise RelationViolationError("Only the registered owner may release itself.")
        before = snapshot.admission_generations[canonical]
        self.registry.unregister(canonical)
        after = self.registry.snapshot().admission_generations[canonical]
        receipts = self._read_owner_release_receipts()
        receipts[canonical] = {
            "pid": owner.pid,
            "before": before,
            "after": after,
            "thread": json.dumps(replace(owner, active_turn=None).to_wire(), sort_keys=True),
        }
        _atomic_write_text(
            self.root / "owner_release_receipts.json",
            json.dumps(receipts, sort_keys=True),
            fsync_parent=True,
        )

    @staticmethod
    def _process_alive(pid: int) -> bool:
        if sys.platform == "win32":
            import ctypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5  # Access denied still means it exists.
            try:
                code = ctypes.c_uint()
                return (
                    bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                    and code.value == 259
                )
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            # An inaccessible PID is not proof of death. Never unregister a
            # possibly live owner merely because its liveness probe failed.
            return True
        if sys.platform.startswith("linux"):
            try:
                stat = Path(f"/proc/{pid}/stat").read_text().split()
            except FileNotFoundError:
                return False
            except OSError:
                return True
            return not (len(stat) > 2 and stat[2] == "Z")
        try:
            # A caller may deliberately clear PATH (including CLI subprocesses).
            # Do not mistake failure to locate ps for a dead macOS owner.
            probe = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "stat="],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return True
        if probe.returncode != 0:
            return True  # Unknown, not an authoritative dead-process receipt.
        return not probe.stdout.strip().startswith("Z")

    def _is_local_participant(self, thread: Thread, *, wait: bool = True) -> bool:
        """Prove a PID belongs to the named participant before signaling it."""
        if sys.platform == "darwin":
            import socket

            from .runtime import socket_path

            deadline = time.monotonic() + (2 if wait else 0)
            while True:
                try:
                    with socket.socket(socket.AF_UNIX) as connection:
                        connection.settimeout(0.5)
                        connection.connect(str(socket_path(self.root, thread.pid)))
                        # SOL_LOCAL / LOCAL_PEERPID: kernel-authenticated owner PID.
                        return bool(connection.getsockopt(0, 2) == thread.pid)
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.05)
                except OSError:
                    return False
        if not sys.platform.startswith("linux"):
            return False
        try:
            entries = Path(f"/proc/{thread.pid}/environ").read_bytes().split(b"\0")
        except OSError:
            return False
        environ = dict(item.split(b"=", 1) for item in entries if item and b"=" in item)
        expected = {name.encode() for name in self.registry.aliases_for(thread.name)}
        name_matches = any(
            item.startswith((b"AGENT_COMMS_THREAD=", b"PI_AGENT_ID="))
            and item.split(b"=", 1)[1] in expected
            for item in entries
        )
        root = Path(environ.get(b"AGENT_COMMS_ROOT", b"~/.agent-comms").decode()).expanduser()
        if root.resolve() != self.root.resolve():
            return False
        if name_matches:
            return True
        # ACP owners created before their generated identity was known prove
        # ownership via their wire-scoped Unix socket and kernel credentials.
        import socket
        import struct

        from .runtime import socket_path

        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(0.5)
                connection.connect(str(socket_path(self.root, thread.pid)))
                pid, uid, _ = struct.unpack(
                    "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                return bool(pid == thread.pid and uid == os.getuid())
        except OSError:
            return False
