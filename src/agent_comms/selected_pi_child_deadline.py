"""Linux-only, operation-dedicated selected-Pi RPC child containment.

This is NOT a grant to select a paid model. The caller must separately attest
model/auth/extension parity, hold the saved-session lock and journal UNKNOWN.
No reusable Pi process is accepted. Unsupported containment fails before spawn.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import platform
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _pidfd_open(pid: int) -> int:
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    if platform.machine() not in {"x86_64", "aarch64"}:
        raise OSError("Unsupported pidfd syscall architecture")
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(434, pid, 0)  # Linux pidfd_open on x86_64/aarch64.
    if result < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open failed")
    return int(result)


def _pidfd_kill(fd: int) -> None:
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(424, fd, signal.SIGKILL, 0, 0) < 0:
        raise OSError(ctypes.get_errno(), "pidfd_send_signal failed")


class SelectedChildUnknown(RuntimeError):  # noqa: N818 - UNKNOWN is a protocol state
    """The exact operation may have started; do not retry or send original input."""


@dataclass(frozen=True)
class SelectedIncarnation:
    owner: str
    session: str
    operation: str
    token: str
    deadline_ns: int


@dataclass
class ArmedSelectedChild:
    identity: SelectedIncarnation
    guardian: asyncio.subprocess.Process
    receipt: Path
    _retire_task: asyncio.Task[dict[str, Any]] | None = field(default=None, init=False)
    _sent: bool = field(default=False, init=False)
    _send_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    async def send(self, request: bytes) -> bytes:
        """One bounded RPC line; no command can cross the absolute deadline."""
        if not request.endswith(b"\n") or len(request) > 131072:
            raise ValueError("Bounded selected RPC line required")
        # Consume even across concurrent callers before any write or await.
        # A failed drain/read is UNKNOWN, not authorization to re-send.
        with self._send_lock:
            if self._sent or self._retire_task is not None:
                raise SelectedChildUnknown("Selected operation already dispatched or retired")
            self._sent = True
        if time.monotonic_ns() >= self.identity.deadline_ns:
            raise SelectedChildUnknown("Selected child deadline elapsed")
        assert self.guardian.stdin is not None and self.guardian.stdout is not None
        try:
            self.guardian.stdin.write(request)
            remaining = (self.identity.deadline_ns - time.monotonic_ns()) / 1e9
            async with asyncio.timeout(max(0, remaining)):
                await self.guardian.stdin.drain()
                response = await self.guardian.stdout.readline()
            if not response or len(response) > 270000:
                raise SelectedChildUnknown("Selected child response missing or excessive")
            return response
        except (TimeoutError, OSError, BrokenPipeError) as error:
            raise SelectedChildUnknown("Selected child RPC uncertain") from error

    async def retire(self) -> dict[str, Any]:
        """Close the exact proxy; cancellation cannot discard the join handle."""
        with self._send_lock:
            if self._retire_task is None:
                self._sent = True  # A retired capability cannot be dispatched.
                if self.guardian.stdin is not None:
                    self.guardian.stdin.close()
                self._retire_task = asyncio.create_task(self._join_exact())
            task = self._retire_task
        return await asyncio.shield(task)

    async def _join_exact(self) -> dict[str, Any]:
        try:
            await asyncio.wait_for(self.guardian.wait(), timeout=4)
        except TimeoutError as error:
            # Dedicated guardian only, never a reusable/shared child. Its
            # pdeathsig kills its unshare parent; --kill-child kills PID 1.
            self.guardian.kill()
            await self.guardian.wait()
            raise SelectedChildUnknown("Guardian failed to join") from error
        try:
            row = json.loads(self.receipt.read_text())
            if not isinstance(row, dict):
                raise ValueError("Invalid selected operation receipt")
            if row.get("token") != self.identity.token or row.get("status") != "unknown":
                raise ValueError("Wrong selected operation receipt")
            namespace = row.get("namespace")
            if namespace is not None:
                if (
                    not isinstance(namespace, str)
                    or re.fullmatch(r"pid:\[[0-9]+\]", namespace) is None
                ):
                    raise ValueError("Invalid exact namespace identity")
                try:
                    async with asyncio.timeout(3):
                        while _namespace_has_live_processes(namespace):
                            await asyncio.sleep(0.01)
                except TimeoutError as error:
                    raise SelectedChildUnknown(
                        "Selected namespace not retired; slot poisoned"
                    ) from error
            return dict(row)
        except (OSError, ValueError) as error:
            raise SelectedChildUnknown("Missing selected child outcome") from error


def _namespace_has_live_processes(namespace: str) -> bool:
    """Check exact PID namespace members after guardian exit; never rely on leader wait."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if os.readlink(entry / "ns/pid") != namespace:
                continue
            # Zombie processes have completed; PID1/descendants still alive
            # make this true until exact kernel teardown is observed.
            state = (entry / "stat").read_text().rsplit(") ", 1)[1].split()[0]
            if state != "Z":
                return True
        except FileNotFoundError:
            continue
        except PermissionError as error:
            raise SelectedChildUnknown("Cannot attest namespace retirement") from error
    return False


async def arm_selected_child(
    *,
    owner: str,
    session: str,
    operation: str,
    command: tuple[str, ...],
    deadline_ns: int,
    receipt: Path,
) -> ArmedSelectedChild:
    """Arm a dedicated watchdog BEFORE the native argv can be executed.

    The caller must hold its persistent-session lock, retain the returned exact
    handle until retire is joined and durably record UNKNOWN if receipt absent.
    The guardian owns its own session, namespace and independent monotonic clock.
    """
    if (
        sys.platform != "linux"
        or not shutil.which("unshare")
        or platform.machine() not in {"x86_64", "aarch64"}
    ):
        raise SelectedChildUnknown("PID-namespace containment unavailable")
    if (
        not all(isinstance(v, str) and 0 < len(v) <= 256 for v in (owner, session, operation))
        or not command
        or len(command) > 32
        or any(not isinstance(v, str) or not v or len(v) > 16384 for v in command)
        or deadline_ns - time.monotonic_ns() <= 0
        or deadline_ns - time.monotonic_ns() > 90_000_000_000
        or not receipt.is_absolute()
        or receipt.exists()
        or not receipt.parent.is_dir()
    ):
        raise ValueError("Exact bounded selected-child authority required")
    identity = SelectedIncarnation(owner, session, operation, secrets.token_hex(16), deadline_ns)
    spec = json.dumps(
        {"identity": vars_identity(identity), "command": command, "receipt": str(receipt)},
        separators=(",", ":"),
    )
    guardian = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent_comms.selected_pi_child_deadline",
        "--guardian",
        spec,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert guardian.stdout is not None
    try:
        remaining = max(0, (deadline_ns - time.monotonic_ns()) / 1e9)
        async with asyncio.timeout(min(5, remaining)):
            line = await guardian.stdout.readline()
        if json.loads(line).get("armed") != identity.token:
            raise SelectedChildUnknown("Missing exact guardian armed proof")
        return ArmedSelectedChild(identity, guardian, receipt)
    except BaseException:
        # If the acknowledgement was lost, provider might have started.
        # Independent deadline survives cancellation and owner loss.
        if guardian.stdin is not None:
            guardian.stdin.close()
        raise SelectedChildUnknown("Selected-child arm UNKNOWN; no dispatch") from None


def vars_identity(identity: SelectedIncarnation) -> dict[str, Any]:
    return {
        name: getattr(identity, name)
        for name in ("owner", "session", "operation", "token", "deadline_ns")
    }


def _record(
    path: Path, identity: SelectedIncarnation, status: str, namespace: str | None = None
) -> None:
    data = json.dumps(
        {
            "owner": identity.owner,
            "session": identity.session,
            "operation": identity.operation,
            "token": identity.token,
            "status": status,
            **({"namespace": namespace} if namespace is not None else {}),
        },
        separators=(",", ":"),
    ).encode()
    tmp = path.with_name(path.name + "." + identity.token + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _parent_death(expected: int) -> None:
    # Applied only to the NEW unshare intermediary; kernel signals it if the
    # guardian disappears. unshare --kill-child=SIGKILL then kills namespace PID1.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != expected:
        os._exit(127)


def _guardian(spec: str) -> None:
    data = json.loads(spec)
    identity = SelectedIncarnation(**data["identity"])
    receipt = Path(data["receipt"])
    command = tuple(data["command"])
    if time.monotonic_ns() >= identity.deadline_ns:
        _record(receipt, identity, "unknown")
        return
    # An armed record is durable before creating any native/provider process.
    _record(receipt, identity, "unknown")
    stop = threading.Event()
    guardian_pid = os.getpid()
    pidfd_holder: list[int | None] = [None]

    def deadline() -> None:
        remaining = max(0, (identity.deadline_ns - time.monotonic_ns()) / 1e9)
        if not stop.wait(remaining):
            fd = pidfd_holder[0]
            if fd is None:
                # Popen still blocked: wrapper cannot pass the release gate.
                # Killing this dedicated guardian triggers intermediary
                # PDEATHSIG if it was spawned before Popen returned.
                os.kill(guardian_pid, signal.SIGKILL)
            else:
                with suppress(ProcessLookupError):
                    _pidfd_kill(fd)

    watcher = threading.Thread(target=deadline, daemon=True)
    watcher.start()  # Independent watchdog BEFORE the unshare Popen boundary.
    proc: subprocess.Popen[bytes] | None = None
    try:
        ready_r, ready_w = os.pipe()
        go_r, go_w = os.pipe()
        child_spec = json.dumps(
            {"identity": vars_identity(identity), "command": command}, separators=(",", ":")
        )
        proc = subprocess.Popen(
            [
                "unshare",
                "--user",
                "--map-root-user",
                "--pid",
                "--fork",
                "--kill-child=SIGKILL",
                "--",
                sys.executable,
                "-m",
                "agent_comms.selected_pi_child_deadline",
                "--namespace-child",
                child_spec,
                str(ready_w),
                str(go_r),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            pass_fds=(ready_w, go_r),
            preexec_fn=lambda: _parent_death(guardian_pid),
        )
        os.close(ready_w)
        os.close(go_r)
        pidfd = _pidfd_open(proc.pid)
        pidfd_holder[0] = pidfd

        def kill_exact() -> None:
            with suppress(ProcessLookupError):
                _pidfd_kill(pidfd)

        try:
            # A durable armed receipt alone is not namespace readiness. Do not
            # acknowledge the caller until the actual PID namespace init has
            # proven PID 1 and a distinct namespace, while the pidfd is alive.
            with selectors.DefaultSelector() as selector:
                selector.register(ready_r, selectors.EVENT_READ)
                selector.register(0, selectors.EVENT_READ)
                ready = b""
                while b"\n" not in ready:
                    remaining = (identity.deadline_ns - time.monotonic_ns()) / 1e9
                    if remaining <= 0 or proc.poll() is not None:
                        raise SelectedChildUnknown("Namespace startup failed before readiness")
                    for key, _ in selector.select(timeout=min(0.1, remaining)):
                        if key.fd == 0:
                            # Owner exited/cancelled or illicit pre-arm input.
                            raise SelectedChildUnknown("Owner lost during namespace startup")
                        part = os.read(ready_r, 4096)
                        if not part or len(ready) + len(part) > 4096:
                            raise SelectedChildUnknown("Namespace readiness missing")
                        ready += part
            proof = json.loads(ready)
            if (
                proof != {"ready": identity.token, "pid": 1, "namespace": proof.get("namespace")}
                or not isinstance(proof["namespace"], str)
                or proof["namespace"] == os.readlink("/proc/self/ns/pid")
                or time.monotonic_ns() >= identity.deadline_ns
                or proc.poll() is not None
            ):
                raise SelectedChildUnknown("Namespace readiness unverified")
            _record(receipt, identity, "unknown", namespace=proof["namespace"])
            os.write(1, (json.dumps({"armed": identity.token}) + "\n").encode())
            os.write(go_w, b"G")
        finally:
            os.close(ready_r)
            os.close(go_w)

        def copy_stdout() -> None:
            assert proc is not None and proc.stdout is not None
            try:
                while chunk := os.read(proc.stdout.fileno(), 65536):
                    os.write(1, chunk)
            except OSError:
                kill_exact()

        forward = threading.Thread(target=copy_stdout, daemon=True)
        forward.start()
        assert proc.stdin is not None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(0, selectors.EVENT_READ)
                while proc.poll() is None:
                    remaining = (identity.deadline_ns - time.monotonic_ns()) / 1e9
                    if remaining <= 0:
                        break
                    if not selector.select(timeout=min(0.1, remaining)):
                        continue
                    chunk = os.read(0, 65536)
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
        except (OSError, BrokenPipeError):
            pass
        finally:
            # Owner EOF/cancellation/deadline always retires exact namespace.
            kill_exact()
        proc.wait()
        stop.set()
        forward.join(timeout=1)
        watcher.join(timeout=1)
        os.close(pidfd)
    except BaseException:
        if proc is not None:
            with suppress(ProcessLookupError):
                proc.kill()
            proc.wait()
        raise


def _namespace_child(spec: str, ready_fd: int, go_fd: int) -> None:
    data = json.loads(spec)
    identity = SelectedIncarnation(**data["identity"])
    try:
        if os.getpid() != 1 or time.monotonic_ns() >= identity.deadline_ns:
            raise SelectedChildUnknown("Not the selected PID namespace init")
        namespace = os.readlink("/proc/self/ns/pid")
        os.write(
            ready_fd,
            (
                json.dumps({"ready": identity.token, "pid": os.getpid(), "namespace": namespace})
                + "\n"
            ).encode(),
        )
        os.close(ready_fd)
        if os.read(go_fd, 1) != b"G" or time.monotonic_ns() >= identity.deadline_ns:
            raise SelectedChildUnknown("Selected namespace release missing")
        os.close(go_fd)
        command = tuple(data["command"])
        os.execvpe(command[0], command, os.environ)
    except BaseException:
        os._exit(127)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--guardian":
        _guardian(sys.argv[2])
    elif len(sys.argv) == 5 and sys.argv[1] == "--namespace-child":
        _namespace_child(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
