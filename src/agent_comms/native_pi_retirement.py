"""Default-OFF Linux PID-namespace native-child retirement prototype.

No caller receives an authority token merely from RPC text, a process-group
wait, or a visible journal row. This module never clears an input disposition.
The exact caller must independently corroborate a returned one-use receipt.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
import re
import signal
import socket
import stat
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .native_pi import NativeContextProof, NativePiRpcLaunch

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PR_SET_PDEATHSIG = 1


class RetirementUnavailable(RuntimeError):  # noqa: N818 - nominal unavailable outcome
    """No authoritative returned terminal receipt; preserve UNKNOWN."""


@dataclass(frozen=True, slots=True)
class RetirementIdentity:
    wire_root_id: str
    input_id: str
    stage: str
    claim_id: str
    execution_id: str | None
    attempt_ordinal: int | None
    owner_thread: str
    recipient_lookup: str
    owner_created_at: float
    owner_pid: int
    owner_generation: int
    owner_admission_epoch: int
    owner_turn_id: str
    source_seq: int
    source_message_id: str
    native_request_digest: str
    session_file: Path

    def check(self, input_id: str, prompt: str, session_file: Path | None) -> None:
        from .native_prompt_binding import native_request_digest

        if (
            not all(
                _HEX32.fullmatch(value)
                for value in (self.wire_root_id, self.input_id, self.recipient_lookup)
            )
            or self.input_id != input_id
            or self.stage not in ("full", "triage")
            or not self.claim_id
            or not self.owner_thread
            or type(self.owner_created_at) is not float
            or not math.isfinite(self.owner_created_at)
            or self.owner_created_at <= 0
            or type(self.owner_pid) is not int
            or self.owner_pid != os.getpid()
            or not self.owner_turn_id
            or not self.source_message_id
            or type(self.owner_generation) is not int
            or self.owner_generation < 1
            or type(self.owner_admission_epoch) is not int
            or self.owner_admission_epoch < 1
            or type(self.source_seq) is not int
            or self.source_seq < 1
            or not _HEX64.fullmatch(self.native_request_digest)
            or self.native_request_digest != native_request_digest(prompt)
            or (self.stage == "full")
            != (self.execution_id is not None and self.attempt_ordinal is not None)
            or session_file is None
            or self.session_file != session_file
        ):
            raise RetirementUnavailable("exact prewrite native identity is unavailable")


@dataclass(frozen=True, slots=True)
class RetiredNativeInputReceipt:
    """Ephemeral one-use result, not an ACK or provider-consumption proof."""

    _record: dict[str, object] = field(repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)
    _used: list[bool] = field(default_factory=lambda: [False], compare=False, repr=False)

    def take_once(self, expected: RetirementIdentity) -> dict[str, object]:
        with self._lock:
            if self._used[0]:
                raise RetirementUnavailable("native retirement receipt was already used")
            self._used[0] = True
            if self._record.get("identity") != _identity_record(expected):
                raise RetirementUnavailable("native retirement receipt identity differs")
            return dict(self._record)


def _identity_record(identity: RetirementIdentity) -> dict[str, object]:
    return {
        name: str(value) if isinstance(value, Path) else value
        for name, value in (
            (name, getattr(identity, name)) for name in identity.__dataclass_fields__
        )
    }


def _pidfd_open(pid: int) -> int:
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.pidfd_open(ctypes.c_int(pid), ctypes.c_uint(0))
    if result < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open unavailable")
    return int(result)


def _pidfd_kill(pidfd: int) -> None:
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
    else:
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.pidfd_send_signal(ctypes.c_int(pidfd), ctypes.c_int(signal.SIGKILL), None, 0)
        if result < 0:
            raise OSError(ctypes.get_errno(), "pidfd_send_signal unavailable")


def _arm_parent_death() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0):
        raise RetirementUnavailable("namespace parent-death watchdog cannot arm")


def _wrapper() -> None:
    expected = int(os.environ["AC_RETIRE_PARENT_PID"])
    _arm_parent_death()
    if os.getppid() != expected:
        raise RetirementUnavailable("retirement launcher lost its parent")
    os.execvp(
        "unshare",
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--pid",
            "--fork",
            "--mount-proc",
            "--kill-child",
            "--",
            sys.executable,
            "-m",
            "agent_comms.native_pi_retirement",
            "init",
        ],
    )


def _namespace_init() -> None:
    if os.getpid() != 1:
        raise RetirementUnavailable("retirement launcher is not namespace PID 1")
    _arm_parent_death()
    sock = socket.socket(fileno=int(os.environ["AC_RETIRE_CTL_FD"]))
    nonce = os.environ["AC_RETIRE_NONCE"]
    sock.send(json.dumps({"kind": "armed", "nonce": nonce}).encode())
    sock.settimeout(5)
    command = json.loads(sock.recv(4096))
    if command != {"kind": "start", "nonce": nonce}:
        raise RetirementUnavailable("native PID 1 was not explicitly dispatched")
    argv = json.loads(os.environ["AC_RETIRE_CHILD_ARGV"])
    if type(argv) is not list or not argv or any(type(item) is not str for item in argv):
        raise RetirementUnavailable("native PID 1 child argv is invalid")
    import subprocess

    # Child inherits the exact parent's RPC pipes. PID 1 stays alive after a
    # child exit until the parent kills PID 1 via its verified pidfd.
    child = subprocess.Popen(argv, stdin=None, stdout=None, stderr=None)
    sock.send(json.dumps({"kind": "started", "nonce": nonce, "child_pid": child.pid}).encode())
    while True:
        signal.pause()


def _credentials(sock: socket.socket) -> tuple[dict[str, object], int]:
    raw, ancillary, _, _ = sock.recvmsg(4096, 256)
    observed = [
        int.from_bytes(item[:4], sys.byteorder, signed=True)
        for level, kind, item in ancillary
        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(item) >= 12
    ]
    if len(observed) != 1:
        raise RetirementUnavailable("namespace init has no unique kernel peer credentials")
    try:
        record = json.loads(raw)
    except ValueError as error:
        raise RetirementUnavailable("namespace init handshake is malformed") from error
    if type(record) is not dict:
        raise RetirementUnavailable("namespace init handshake is not an object")
    return record, observed[0]


def _session_identity(path: Path) -> tuple[int, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or (path.stat().st_dev, path.stat().st_ino) != (info.st_dev, info.st_ino)
            ):
                raise RetirementUnavailable("prewrite native session inode is untrusted")
            return info.st_dev, info.st_ino
        finally:
            os.close(fd)
    except OSError as error:
        raise RetirementUnavailable("prewrite native session inode is unavailable") from error


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_private_file(path: Path, expected_inode: tuple[int, int] | None = None) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or (expected_inode is not None and (info.st_dev, info.st_ino) != expected_inode)
            or (path.stat().st_dev, path.stat().st_ino) != (info.st_dev, info.st_ino)
        ):
            raise RetirementUnavailable("native proof inode changed before parent fsync")
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(slots=True)
class RetiredChild:
    process: asyncio.subprocess.Process
    sock: socket.socket
    pidfd: int
    init_pid: int
    namespace_inode: int
    session_inode: tuple[int, int]
    identity: RetirementIdentity
    session_dir: Path

    async def retire(self, proof: NativeContextProof | None) -> RetiredNativeInputReceipt | None:
        from .native_pi import NativePiUnavailable

        try:
            _pidfd_kill(self.pidfd)
            await asyncio.wait_for(self.process.wait(), timeout=3)
            loop = asyncio.get_running_loop()
            # pidfd readiness and vanished host proc prove PID 1 exited and
            # was reaped; kernel PID-namespace teardown kills its descendants.
            if not await loop.run_in_executor(None, self._init_gone):
                return None
            if proof is None or proof.input_id != self.identity.input_id:
                return None
            if proof.session_file != self.identity.session_file:
                return None
            if _session_identity(proof.session_file) != self.session_inode:
                return None
            if proof.request_generation < 1 or not proof.session_id or not proof.session_entry_id:
                return None
            if not _HEX64.fullmatch(proof.llm_context_digest):
                return None
            # The child can emit a complete row without fsync. Parent fsyncs
            # the pinned session and exact proof file only AFTER child death,
            # then re-reads the live-correlated evidence before any receipt.
            from .native_pi import _read_native_context_evidence

            _sync_private_file(proof.session_file, self.session_inode)
            _sync_private_file(Path(str(proof.session_file) + ".input-proof"))
            _sync_directory(self.session_dir)
            if _read_native_context_evidence(proof.session_file, self.identity.input_id) != proof:
                return None
            terminal = self.session_dir / "native-retirement-terminal"
            terminal.mkdir(mode=0o700, exist_ok=True)
            _sync_directory(self.session_dir)
            record: dict[str, object] = {
                "version": 1,
                "identity": _identity_record(self.identity),
                "session_dev_ino": list(self.session_inode),
                "session_id": proof.session_id,
                "session_entry_id": proof.session_entry_id,
                "request_generation": proof.request_generation,
                "llm_context_digest": proof.llm_context_digest,
                "namespace_inode": self.namespace_inode,
                "init_host_pid": self.init_pid,
                "nonce": uuid4().hex,
            }
            target = terminal / f"{self.identity.input_id}.json"
            fd = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                payload = (json.dumps(record, sort_keys=True) + "\n").encode()
                while payload:
                    count = os.write(fd, payload)
                    if count <= 0:
                        raise RetirementUnavailable("terminal receipt short write")
                    payload = payload[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
            _sync_directory(terminal)
            return RetiredNativeInputReceipt(record)
        except (OSError, TimeoutError, RetirementUnavailable, NativePiUnavailable, ValueError):
            return None
        finally:
            self.sock.close()
            os.close(self.pidfd)

    def _init_gone(self) -> bool:
        import select

        watcher = select.poll()
        watcher.register(self.pidfd, select.POLLIN)
        return bool(watcher.poll(0)) and not Path(f"/proc/{self.init_pid}").exists()


async def spawn_retired_child(
    launch: NativePiRpcLaunch, identity: RetirementIdentity, input_id: str, prompt: str
) -> RetiredChild:
    """Quarantined historical prototype; no child may launch from this path."""
    # SCM_CREDENTIALS -> procfs -> pidfd_open does not pin PID 1 while its
    # numeric host PID can be recycled. A failed launch could signal another
    # process. Keep this independent guard even for direct callers.
    raise RetirementUnavailable("native Pi namespace retirement is disabled before dispatch")
    if sys.platform != "linux":
        raise RetirementUnavailable("Linux PID namespace and pidfd are required")
    identity.check(input_id, prompt, launch.session_file)
    inode = _session_identity(identity.session_file)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    parent.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    parent.settimeout(3)
    nonce = uuid4().hex
    env = launch.env.copy()
    env.update(
        AC_RETIRE_PARENT_PID=str(os.getpid()),
        AC_RETIRE_CTL_FD=str(child.fileno()),
        AC_RETIRE_NONCE=nonce,
        AC_RETIRE_CHILD_ARGV=json.dumps(launch.argv),
    )
    process: asyncio.subprocess.Process | None = None
    pidfd: int | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_comms.native_pi_retirement",
            "wrapper",
            cwd=str(launch.cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=(child.fileno(),),
            start_new_session=True,
        )
        child.close()
        record, init_pid = await asyncio.to_thread(_credentials, parent)
        if record != {"kind": "armed", "nonce": nonce}:
            raise RetirementUnavailable("namespace watchdog is not armed")
        status = Path(f"/proc/{init_pid}/status").read_text()
        nspid = next(
            (line.split()[1:] for line in status.splitlines() if line.startswith("NSpid:")), ()
        )
        namespace_inode = os.stat(f"/proc/{init_pid}/ns/pid").st_ino
        if (
            len(nspid) < 2
            or nspid[0] != str(init_pid)
            or nspid[-1] != "1"
            or namespace_inode == os.stat("/proc/self/ns/pid").st_ino
            or f"PPid:\t{process.pid}" not in status
        ):
            raise RetirementUnavailable("native child is not dedicated namespace PID 1")
        pidfd = _pidfd_open(init_pid)
        parent.send(json.dumps({"kind": "start", "nonce": nonce}).encode())
        started, sender = await asyncio.to_thread(_credentials, parent)
        if sender != init_pid or started.get("kind") != "started" or started.get("nonce") != nonce:
            raise RetirementUnavailable("namespace child did not start under watchdog")
        child_pid = started.get("child_pid")
        if not isinstance(child_pid, int) or type(child_pid) is not int or child_pid <= 1:
            raise RetirementUnavailable("namespace child launch is not identified")
        return RetiredChild(
            process, parent, pidfd, init_pid, namespace_inode, inode, identity, launch.session_dir
        )
    except BaseException as error:

        async def cleanup_failed_launch() -> None:
            if pidfd is not None:
                with suppress(ProcessLookupError, OSError):
                    _pidfd_kill(pidfd)
                os.close(pidfd)
            if process is not None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            parent.close()
            child.close()

        cleanup_task = asyncio.create_task(cleanup_failed_launch())
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                continue  # never abandon a possibly spawned namespace init
        cleanup_task.result()
        if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        raise RetirementUnavailable("isolated native child launch is unavailable") from error


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    {"wrapper": _wrapper, "init": _namespace_init}[sys.argv[1]]()
