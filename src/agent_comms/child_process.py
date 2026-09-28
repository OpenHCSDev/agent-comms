"""Identity-bound child ownership, process-group retirement and bounded runs.

Linux pidfd operations are lifted from compaction_child_watchdog. Process
identity is captured before releasing the exec gate, including for short jobs.
"""
from __future__ import annotations

import asyncio
import ctypes
import math
import os
import selectors
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .declared_family import DeclaredFamily

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
            member for member in self.members_of_group(leader.pid)
            if member.start_time >= leader.start_time
        )

    @abstractmethod
    def members_of_group(self, pgid: int) -> tuple[ProcessIdentity, ...]: ...

    def send(self, identity: ProcessIdentity, signum: int) -> None:
        self.require(identity)
        os.kill(identity.pid, signum)


class LinuxPlatform(PidfdHandles, PosixPlatform):
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
fd=int(sys.argv[1]); go=os.read(fd,1); os.close(fd)
if go != b'G': sys.exit(126)
os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
"""


@contextmanager
def _launch_gate(command: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], int, int]]:
    if not command:
        raise ValueError("A child command is required")
    # Select capability before spawn. Unsupported platforms cannot silently
    # weaken containment to a direct-child-only launch.
    platform = Platform.current()
    if not isinstance(platform, ProcessGroups):
        raise NotImplementedError("Process-group containment unavailable")
    read_fd, write_fd = os.pipe()
    try:
        yield ((sys.executable, "-c", _EXEC_GATE, str(read_fd), *command), read_fd, write_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)


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
        while self.platform.group_members(self.identity):
            if time.monotonic() >= deadline:
                self.platform.signal_group(self.identity, signal.SIGKILL)
                break
            await asyncio.sleep(0.02)
        outcome = await self.wait()
        # Leader exit alone is insufficient. A grandchild may ignore TERM.
        while self.platform.group_members(self.identity):
            self.platform.signal_group(self.identity, signal.SIGKILL)
            if time.monotonic() >= deadline + STOP_GRACE_SECONDS:
                raise RuntimeError("Child process group did not retire")
            await asyncio.sleep(0.02)
        return outcome

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
        cls, command: tuple[str, ...], *, cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> AttachedChild:
        with _launch_gate(command) as (argv, read_fd, write_fd):
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, pass_fds=(read_fd,),
            )
            identity = ProcessIdentity.capture(process.pid)
            os.write(write_fd, b"G")
        return cls(process, identity)

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    async def wait(self) -> ChildOutcome:
        return ChildOutcome.from_returncode(await self.process.wait())


class BoundedRun:
    @classmethod
    async def run(
        cls, command: tuple[str, ...], *, timeout: float,
        input: bytes | None = None, cwd: str | Path | None = None,
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
        cls, command: tuple[str, ...], *, cwd: str | Path | None = None,
        env: dict[str, str] | None = None, output: Any = subprocess.DEVNULL,
    ) -> DetachedProcess:
        with _launch_gate(command) as (argv, read_fd, write_fd):
            process = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=output, start_new_session=True,
                pass_fds=(read_fd,),
            )
            identity = ProcessIdentity.capture(process.pid)
            os.write(write_fd, b"G")
        return cls(identity, process)

    @classmethod
    def attach(cls, identity: ProcessIdentity) -> DetachedProcess:
        return cls(identity)

    def signal(self, signum: int) -> None:
        self.platform.require(self.identity)
        self.platform.signal_group(self.identity, signum)

    async def stop(self) -> ChildOutcome:
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
