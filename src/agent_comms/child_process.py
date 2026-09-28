"""Identity-bound child ownership, process-group retirement and bounded runs.

Linux pidfd operations are lifted from compaction_child_watchdog. Process
identity is captured before releasing the exec gate, including for short jobs.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec

STOP_GRACE_SECONDS = 2.0


class IdentityMismatchError(ProcessLookupError):
    """A recorded process incarnation is absent or has been replaced."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: int

    def __post_init__(self) -> None:
        if self.pid <= 0 or self.start_time <= 0:
            raise ValueError("A positive PID and platform start time are required")

    @classmethod
    def capture(cls, pid: int) -> ProcessIdentity:
        return Platform.current().identity(pid)

    def alive(self) -> bool:
        return Platform.current().matches(self)


class ProcessGroups(ABC):
    @abstractmethod
    def group_members(self, leader: ProcessIdentity) -> tuple[ProcessIdentity, ...]: ...


class PidfdHandles:
    @staticmethod
    def open_pidfd(pid: int) -> int:
        """Use libc when a portable Python build omitted Linux wrappers."""
        if hasattr(os, "pidfd_open"):
            return os.pidfd_open(pid)
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, "pidfd_open", None)
        if function is None:
            raise NotImplementedError("Linux libc pidfd_open unavailable")
        function.argtypes = (ctypes.c_int, ctypes.c_uint)
        function.restype = ctypes.c_int
        descriptor = function(pid, 0)
        if descriptor < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return int(descriptor)

    @staticmethod
    def signal_pidfd(pidfd: int, sig: int) -> None:
        if hasattr(signal, "pidfd_send_signal"):
            signal.pidfd_send_signal(pidfd, sig)
            return
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, "pidfd_send_signal", None)
        if function is None:
            raise NotImplementedError("Linux libc pidfd_send_signal unavailable")
        function.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
        function.restype = ctypes.c_int
        if function(pidfd, sig, None, 0) < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))

    def watch_deadline(self, pidfd: int, deadline: float) -> None:
        """Exact-process deadline, reusable by the independent watchdog entry."""
        if not math.isfinite(deadline):
            raise ValueError("Finite absolute deadline required")
        os.fstat(pidfd)
        with selectors.DefaultSelector() as selector:
            selector.register(pidfd, selectors.EVENT_READ)
            while (remaining := deadline - time.monotonic()) > 0:
                if selector.select(remaining):
                    return
        with suppress(ProcessLookupError):
            self.signal_pidfd(pidfd, signal.SIGKILL)


@dataclass(frozen=True)
class NamespaceReady:
    token: str
    namespace: str
    pid: int


@dataclass(frozen=True)
class NamespaceLaunch:
    command: tuple[str, ...]
    token: str
    deadline: float


class NamespaceContainment:
    """Linux namespace launch and typed, gated PID-1 readiness proof."""

    @staticmethod
    def namespace_argv(launch: NamespaceLaunch, ready_fd: int, release_fd: int) -> tuple[str, ...]:
        unshare = shutil.which("unshare")
        if unshare is None:
            raise NotImplementedError("PID namespace containment requires unshare")
        return (
            unshare,
            "--user",
            "--map-root-user",
            "--pid",
            "--fork",
            "--kill-child=SIGKILL",
            "--",
            *NamespaceInitCommand(launch, ready_fd, release_fd).argv(),
        )

    @staticmethod
    def namespace_alive(namespace: str) -> bool:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                if os.readlink(entry / "ns/pid") != namespace:
                    continue
                state = (entry / "stat").read_text().rsplit(") ", 1)[1].split()[0]
                if state not in {"Z", "X"}:
                    return True
            except FileNotFoundError:
                continue
        return False


class Platform(DeclaredFamily, affix="Platform"):
    @classmethod
    def current(cls) -> Platform:
        return cls.decode(sys.platform)()

    @abstractmethod
    def identity(self, pid: int) -> ProcessIdentity: ...

    def matches(self, identity: ProcessIdentity) -> bool:
        try:
            return self.identity(identity.pid) == identity
        except ProcessLookupError:
            return False

    def require(self, identity: ProcessIdentity) -> None:
        if not self.matches(identity):
            raise IdentityMismatchError(f"Process incarnation changed: {identity.pid}")

    @abstractmethod
    def send(self, identity: ProcessIdentity, signum: int) -> None: ...

    @abstractmethod
    def group_members(self, leader: ProcessIdentity) -> tuple[ProcessIdentity, ...]: ...

    def signal_group(self, leader: ProcessIdentity, signum: int) -> None:
        # Snapshot identities, then signal each exact member. Never signal a
        # numeric group after its leader may have been reaped and reused.
        for member in self.group_members(leader):
            with suppress(ProcessLookupError):
                self.send(member, signum)


class PosixPlatform(ProcessGroups, Platform):
    def group_members(self, leader: ProcessIdentity) -> tuple[ProcessIdentity, ...]:
        try:
            current = self.identity(leader.pid)
        except ProcessLookupError:
            current = None
        if current is not None and current != leader:
            raise IdentityMismatchError(f"Process group leader changed: {leader.pid}")
        return tuple(
            member
            for member in self.members_of_group(leader.pid)
            if member.start_time >= leader.start_time
        )

    @abstractmethod
    def members_of_group(self, pgid: int) -> tuple[ProcessIdentity, ...]: ...

    def send(self, identity: ProcessIdentity, signum: int) -> None:
        self.require(identity)
        os.kill(identity.pid, signum)


class LinuxPlatform(NamespaceContainment, PidfdHandles, PosixPlatform):
    @staticmethod
    def _stat(pid: int) -> tuple[ProcessIdentity, int]:
        try:
            # comm can contain whitespace and ')'; fields after its final ')'
            # start at field 3. Kernel starttime is field 22, pgrp is field 5.
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        except FileNotFoundError:
            raise ProcessLookupError(pid) from None
        if fields[0] in {"Z", "X"}:
            raise ProcessLookupError(pid)
        return ProcessIdentity(pid, int(fields[19])), int(fields[2])

    def identity(self, pid: int) -> ProcessIdentity:
        return self._stat(pid)[0]

    def members_of_group(self, pgid: int) -> tuple[ProcessIdentity, ...]:
        members = []
        for entry in Path("/proc").iterdir():
            if entry.name.isdecimal():
                with suppress(ProcessLookupError):
                    identity, group = self._stat(int(entry.name))
                    if group == pgid:
                        members.append(identity)
        return tuple(members)

    def send(self, identity: ProcessIdentity, signum: int) -> None:
        # Bind first; recheck start time against the pidfd-bound incarnation
        # before signaling. A concurrent exit can never target a reused PID.
        descriptor = self.open_pidfd(identity.pid)
        try:
            self.require(identity)
            self.signal_pidfd(descriptor, signum)
        finally:
            os.close(descriptor)


class DarwinPlatform(PosixPlatform):
    """libproc exposes microsecond birth times, unlike formatted ps output."""

    class ProcessInfo(ctypes.Structure):
        # External Darwin proc_bsdinfo ABI, from xnu/bsd/sys/proc_info.h.
        _fields_ = [
            (name, ctypes.c_uint32)
            for name in (
                "flags",
                "status",
                "xstatus",
                "pid",
                "ppid",
                "uid",
                "gid",
                "ruid",
                "rgid",
                "svuid",
                "svgid",
                "reserved",
            )
        ] + [
            ("comm", ctypes.c_char * 16),
            ("name", ctypes.c_char * 32),
            ("nfiles", ctypes.c_uint32),
            ("pgid", ctypes.c_uint32),
            ("pjobc", ctypes.c_uint32),
            ("tdev", ctypes.c_uint32),
            ("tpgid", ctypes.c_uint32),
            ("nice", ctypes.c_int32),
            ("start_seconds", ctypes.c_uint64),
            ("start_microseconds", ctypes.c_uint64),
        ]

    def __init__(self):
        self.libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.libproc.proc_pidinfo.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        self.libproc.proc_pidinfo.restype = ctypes.c_int
        self.libproc.proc_listpids.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        self.libproc.proc_listpids.restype = ctypes.c_int

    def identity(self, pid: int) -> ProcessIdentity:
        info = self.ProcessInfo()
        size = self.libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info):
            code = ctypes.get_errno()
            if code == 3:  # ESRCH, the actual OS absence result.
                raise ProcessLookupError(pid)
            raise OSError(code, os.strerror(code))
        if info.status == 5:  # SZOMB: exited, awaiting reap.
            raise ProcessLookupError(pid)
        return ProcessIdentity(pid, info.start_seconds * 1_000_000 + info.start_microseconds)

    def members_of_group(self, pgid: int) -> tuple[ProcessIdentity, ...]:
        # proc_listpids returns bytes. Grow until one query fits; never mistake
        # a full buffer for a complete process-group observation.
        capacity = 16
        while True:
            buffer = (ctypes.c_int * capacity)()
            size = self.libproc.proc_listpids(2, pgid, buffer, ctypes.sizeof(buffer))
            if size < 0:
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code))
            if size < ctypes.sizeof(buffer):
                break
            capacity *= 2
        members = []
        for pid in buffer[: size // ctypes.sizeof(ctypes.c_int)]:
            if pid > 0:
                with suppress(ProcessLookupError):
                    members.append(self.identity(pid))
        return tuple(members)


class ChildOutcome(DeclaredFamily, affix="Outcome"):
    @property
    @abstractmethod
    def successful(self) -> bool: ...

    @classmethod
    def from_returncode(cls, code: int) -> ChildOutcome:
        return SignaledOutcome(-code) if code < 0 else ExitedOutcome(code)


@dataclass(frozen=True)
class ExitedOutcome(ChildOutcome):
    code: int

    @property
    def successful(self) -> bool:
        return self.code == 0


@dataclass(frozen=True)
class SignaledOutcome(ChildOutcome):
    signal_number: int

    @property
    def successful(self) -> bool:
        return False


@dataclass(frozen=True)
class TimedOutOutcome(ChildOutcome):
    termination: ChildOutcome

    @property
    def successful(self) -> bool:
        return False


@dataclass(frozen=True)
class GracefulStopOutcome(ChildOutcome):
    termination: ChildOutcome

    @property
    def successful(self) -> bool:
        return self.termination.successful


@dataclass(frozen=True)
class ForcedStopOutcome(GracefulStopOutcome):
    """At least one member required the forced stage, even if leader exited."""


@dataclass(frozen=True)
class FailedToStartOutcome(ChildOutcome):
    error: str

    @property
    def successful(self) -> bool:
        return False


@dataclass(frozen=True)
class ChildResult:
    outcome: ChildOutcome
    stdout: bytes = b""
    stderr: bytes = b""


# The gate establishes identity before even an instantaneous executable can
# exit. It owns no business logic or stored state and preserves exec PID/group.
_EXEC_GATE = """import os,sys
fd=int(sys.argv[1]); error_fd=int(sys.argv[2]); go=os.read(fd,1); os.close(fd)
if go != b'G': sys.exit(126)
os.set_inheritable(error_fd, False)
try: os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
except OSError as error:
    os.write(error_fd, str(error.errno).encode()); os._exit(126)
"""


@contextmanager
def _launch_gate(
    command: tuple[str, ...],
) -> Iterator[tuple[tuple[str, ...], int, int, int, BinaryIO]]:
    if not command:
        raise ValueError("A child command is required")
    # Select capability before spawn. Unsupported platforms cannot silently
    # weaken containment to a direct-child-only launch.
    platform = Platform.current()
    if not isinstance(platform, ProcessGroups):
        raise NotImplementedError("Process-group containment unavailable")
    read_fd, write_fd = os.pipe()
    error_r, error_w = os.pipe()
    error_writer = os.fdopen(error_w, "wb", buffering=0)
    try:
        yield (
            (sys.executable, "-c", _EXEC_GATE, str(read_fd), str(error_w), *command),
            read_fd,
            write_fd,
            error_r,
            error_writer,
        )
    finally:
        error_writer.close()
        for fd in (read_fd, write_fd, error_r):
            os.close(fd)


async def _exec_error(fd: int, command: str) -> None:
    os.set_blocking(fd, False)
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[bytes] = loop.create_future()

    def receive() -> None:
        if not ready.done():
            ready.set_result(os.read(fd, 64))

    loop.add_reader(fd, receive)
    try:
        data = await ready
        if data:
            code = int(data)
            raise OSError(code, os.strerror(code), command)
    finally:
        loop.remove_reader(fd)


class ChildProcess(ABC):
    """The sole stop algorithm; cancellation retains the cleanup task."""

    def __init__(self, identity: ProcessIdentity):
        self.identity = identity
        self.platform = Platform.current()
        self._stop_task: asyncio.Task[ChildOutcome] | None = None

    @property
    def pid(self) -> int:
        return self.identity.pid

    def alive(self) -> bool:
        return self.platform.matches(self.identity)

    @abstractmethod
    async def wait(self) -> ChildOutcome: ...

    async def _stop(self) -> ChildOutcome:
        self.platform.signal_group(self.identity, signal.SIGTERM)
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        stage = GracefulStopOutcome
        while self.platform.group_members(self.identity):
            if time.monotonic() >= deadline:
                stage = ForcedStopOutcome
                self.platform.signal_group(self.identity, signal.SIGKILL)
                break
            await asyncio.sleep(0.02)
        async with asyncio.timeout(STOP_GRACE_SECONDS):
            outcome = await self.wait()
        # Leader exit alone is insufficient. A grandchild may ignore TERM.
        while self.platform.group_members(self.identity):
            self.platform.signal_group(self.identity, signal.SIGKILL)
            if time.monotonic() >= deadline + STOP_GRACE_SECONDS:
                raise RuntimeError("Child process group did not retire")
            await asyncio.sleep(0.02)
        return stage(outcome)

    async def stop(self) -> ChildOutcome:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        return await asyncio.shield(self._stop_task)


class AttachedChild(ChildProcess):
    def __init__(self, process: asyncio.subprocess.Process, identity: ProcessIdentity):
        super().__init__(identity)
        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr

    @classmethod
    async def start(
        cls,
        command: tuple[str, ...],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> AttachedChild:
        with _launch_gate(command) as (argv, read_fd, write_fd, error_r, error_w):
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=(read_fd, error_w.fileno(), *pass_fds),
            )
            error_w.close()
            identity = ProcessIdentity.capture(process.pid)
            child = cls(process, identity)
            try:
                os.write(write_fd, b"G")
                await _exec_error(error_r, command[0])
            except BaseException:
                await child.stop()
                raise
        return child

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    async def wait(self) -> ChildOutcome:
        return ChildOutcome.from_returncode(await self.process.wait())


class BoundedRun:
    @classmethod
    async def run(
        cls,
        command: tuple[str, ...],
        *,
        timeout: float,
        input: bytes | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ChildResult:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("A positive finite timeout is required")
        try:
            child = await AttachedChild.start(command, cwd=cwd, env=env)
        except OSError as error:
            return ChildResult(FailedToStartOutcome(str(error)))
        exchange = asyncio.create_task(child.process.communicate(input))
        try:
            try:
                stdout, stderr = await asyncio.wait_for(asyncio.shield(exchange), timeout)
                outcome = await child.wait()
            except TimeoutError:
                outcome = TimedOutOutcome(await child.stop())
                stdout, stderr = await exchange
            return ChildResult(outcome, stdout, stderr)
        finally:
            await child.stop()
            await exchange


class DetachedProcess(ChildProcess):
    def __init__(self, identity: ProcessIdentity, process: subprocess.Popen[bytes] | None = None):
        super().__init__(identity)
        self._process = process

    @classmethod
    def launch(
        cls,
        command: tuple[str, ...],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        output: Any = subprocess.DEVNULL,
    ) -> DetachedProcess:
        with _launch_gate(command) as (argv, read_fd, write_fd, error_r, error_w):
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
                pass_fds=(read_fd, error_w.fileno()),
            )
            error_w.close()
            identity = ProcessIdentity.capture(process.pid)
            os.write(write_fd, b"G")
            error = os.read(error_r, 64)
            if error:
                process.wait()
                code = int(error)
                raise OSError(code, os.strerror(code), command[0])
        return cls(identity, process)

    @classmethod
    def attach(cls, identity: ProcessIdentity) -> DetachedProcess:
        return cls(identity)

    def signal(self, signum: int) -> None:
        self.platform.require(self.identity)
        self.platform.signal_group(self.identity, signum)

    async def stop(self) -> ChildOutcome:
        if self._stop_task is None:
            self.platform.require(self.identity)
        return await super().stop()

    async def wait(self) -> ChildOutcome:
        if self._process is not None:
            while self._process.poll() is None:
                await asyncio.sleep(0.02)
            return ChildOutcome.from_returncode(self._process.returncode)
        while self.alive():
            await asyncio.sleep(0.02)
        return DetachedExitOutcome()


@dataclass(frozen=True)
class DetachedExitOutcome(ChildOutcome):
    """Exit observed; a non-parent cannot claim an exit code."""

    @property
    def successful(self) -> bool:
        return False


class NamespacedChild(AttachedChild):
    """Attached shape with kernel containment and an independent hard deadline.

    Lifted from the selected-Pi guardian: namespace PID1 attests readiness before
    exec release, a pidfd watchdog arms before launch, and killing unshare kills
    namespace PID1 (including descendants which escaped the process group).
    Business input/UNKNOWN journals stay with the caller.
    """

    def __init__(self, child: AttachedChild, watchdog: AttachedChild, namespace: str):
        super().__init__(child.process, child.identity)
        self.watchdog = watchdog
        self.namespace = namespace

    @classmethod
    async def start(
        cls,
        command: tuple[str, ...],
        *,
        deadline: float,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> NamespacedChild:
        platform = Platform.current()
        if not isinstance(platform, NamespaceContainment) or not isinstance(platform, PidfdHandles):
            raise NotImplementedError("Exact namespace deadline containment unavailable")
        if not math.isfinite(deadline) or deadline <= time.monotonic():
            raise ValueError("A future finite monotonic deadline is required")
        launch = NamespaceLaunch(command, secrets.token_hex(16), deadline)
        ready_r, ready_w = os.pipe()
        release_r, release_w = os.pipe()
        child = watchdog = None
        descriptor = None
        try:
            child = await AttachedChild.start(
                platform.namespace_argv(launch, ready_w, release_r),
                cwd=cwd,
                env=env,
                pass_fds=(ready_w, release_r),
            )
            descriptor = platform.open_pidfd(child.pid)
            watchdog = await AttachedChild.start(
                WatchDeadlineCommand(descriptor, deadline).argv(),
                pass_fds=(descriptor,),
            )
            assert watchdog.stdout is not None
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                if await watchdog.stdout.readline() != b"armed\n":
                    raise RuntimeError("Independent child watchdog failed to arm")
                # Reading this dedicated pipe cannot consume provider stdout.
                os.set_blocking(ready_r, False)
                loop = asyncio.get_running_loop()
                ready: asyncio.Future[bytes] = loop.create_future()
                data = bytearray()

                def receive() -> None:
                    if ready.done():
                        return
                    chunk = os.read(ready_r, 4096)
                    data.extend(chunk)
                    if len(data) > 4096 or not chunk:
                        ready.set_exception(RuntimeError("Namespace readiness missing"))
                    elif b"\n" in data:
                        ready.set_result(bytes(data))

                loop.add_reader(ready_r, receive)
                try:
                    proof = FieldCodec.decode(NamespaceReady, json.loads(await ready))
                finally:
                    loop.remove_reader(ready_r)
                if (
                    proof.token != launch.token
                    or proof.pid != 1
                    or proof.namespace == os.readlink("/proc/self/ns/pid")
                    or not child.alive()
                ):
                    raise RuntimeError("Namespace readiness identity mismatch")
                os.write(release_w, b"G")
                return cls(child, watchdog, proof.namespace)
        except BaseException:
            if child is not None:
                await child.stop()
            if watchdog is not None:
                await watchdog.stop()
            raise
        finally:
            for fd in (ready_r, ready_w, release_r, release_w):
                os.close(fd)
            if descriptor is not None:
                os.close(descriptor)

    async def _stop(self) -> ChildOutcome:
        result = await super()._stop()
        await self.watchdog.stop()
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while NamespaceContainment.namespace_alive(self.namespace):
            if time.monotonic() >= deadline:
                raise RuntimeError("Child PID namespace did not retire")
            await asyncio.sleep(0.02)
        return result


class ChildCommand(DeclaredFamily, affix="Command"):
    """Internal child entrypoints decode once through their declaration owner."""

    def argv(self) -> tuple[str, ...]:
        return (
            sys.executable,
            "-m",
            "agent_comms.child_process",
            json.dumps(FieldCodec.encode(self)),
        )

    @abstractmethod
    def run(self) -> None: ...


@dataclass(frozen=True)
class NamespaceInitCommand(ChildCommand):
    launch: NamespaceLaunch
    ready_fd: int
    release_fd: int

    def run(self) -> None:
        try:
            if os.getpid() != 1 or time.monotonic() >= self.launch.deadline:
                raise RuntimeError("Namespace init missing or deadline elapsed")
            proof = NamespaceReady(
                self.launch.token,
                os.readlink("/proc/self/ns/pid"),
                os.getpid(),
            )
            os.write(self.ready_fd, (json.dumps(FieldCodec.encode(proof)) + "\n").encode())
            os.close(self.ready_fd)
            if os.read(self.release_fd, 1) != b"G" or time.monotonic() >= self.launch.deadline:
                raise RuntimeError("Namespace release absent or deadline elapsed")
            os.close(self.release_fd)
            os.execvpe(self.launch.command[0], self.launch.command, os.environ)
        except BaseException:
            os._exit(127)


@dataclass(frozen=True)
class WatchDeadlineCommand(ChildCommand):
    descriptor: int
    deadline: float

    def run(self) -> None:
        platform = Platform.current()
        if not isinstance(platform, PidfdHandles):
            raise NotImplementedError("Exact process watchdog unavailable")
        os.fstat(self.descriptor)
        print("armed", flush=True)
        platform.watch_deadline(self.descriptor, self.deadline)


if __name__ == "__main__":
    FieldCodec.decode(ChildCommand, json.loads(sys.argv[1])).run()
