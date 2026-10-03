"""Identity-bound child ownership, process-group retirement and bounded runs.

Linux pidfd operations are lifted from compaction_child_watchdog. Process
identity is captured before releasing the exec gate, including for short jobs.
"""

from __future__ import annotations

import asyncio
import ctypes
import io
import json
import math
import os
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import ExitStack, asynccontextmanager, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .sealed import Sealed

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
    store_lock_interval = 0.025

    @classmethod
    def current(cls) -> Platform:
        return cls.decode(sys.platform)()

    @abstractmethod
    def try_store_lock(self, descriptor: int, *, shared: bool) -> None:
        """One nonblocking native attempt; busy is BlockingIOError."""

    def acquire_store_lock(self, descriptor: int, *, shared: bool, blocking: bool) -> None:
        """Use the common physical wait driver when the platform needs polling."""
        from .store_files import StoreLockContention

        StoreLockContention(math.inf if blocking else 0).acquire(descriptor, self, shared=shared)

    def release_store_lock(self, descriptor: int) -> None:
        """POSIX custody ends at last close, including inherited descriptors."""

    @abstractmethod
    def launch(self, command: tuple[str, ...], pass_fds: tuple[int, ...]) -> ChildLaunch: ...

    @abstractmethod
    def terminate_group(self, identity: ProcessIdentity) -> None: ...

    @abstractmethod
    def force_group(self, identity: ProcessIdentity) -> None: ...

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
    def acquire_store_lock(self, descriptor: int, *, shared: bool, blocking: bool) -> None:
        import fcntl

        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(descriptor, mode | (0 if blocking else fcntl.LOCK_NB))

    def try_store_lock(self, descriptor: int, *, shared: bool) -> None:
        import fcntl

        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(descriptor, mode | fcntl.LOCK_NB)

    def launch(self, command: tuple[str, ...], pass_fds: tuple[int, ...]) -> ChildLaunch:
        return PosixLaunch(command, pass_fds)

    def terminate_group(self, identity: ProcessIdentity) -> None:
        self.signal_group(identity, signal.SIGTERM)

    def force_group(self, identity: ProcessIdentity) -> None:
        self.signal_group(identity, signal.SIGKILL)

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


class Win32Platform(ProcessGroups, Platform):
    """Kernel creation times and named job objects bind the complete child tree."""

    store_lock_interval = 0.01

    def try_store_lock(self, descriptor: int, *, shared: bool) -> None:
        import errno
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBRLCK if shared else msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EDEADLK}:
                raise
            raise BlockingIOError(error.errno, error.strerror) from error

    def release_store_lock(self, descriptor: int) -> None:
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("size", ctypes.c_uint32),
            ("usage", ctypes.c_uint32),
            ("tid", ctypes.c_uint32),
            ("owner", ctypes.c_uint32),
            ("base_priority", ctypes.c_int32),
            ("delta_priority", ctypes.c_int32),
            ("flags", ctypes.c_uint32),
        ]

    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, dword, pointer = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p
        self._api("OpenProcess", handle, dword, ctypes.c_int, dword)
        self._api("CloseHandle", ctypes.c_int, handle)
        self._api("GetProcessTimes", ctypes.c_int, handle, pointer, pointer, pointer, pointer)
        self._api("WaitForSingleObject", dword, handle, dword)
        self._api("CreateJobObjectW", handle, pointer, ctypes.c_wchar_p)
        self._api("OpenJobObjectW", handle, dword, ctypes.c_int, ctypes.c_wchar_p)
        self._api("AssignProcessToJobObject", ctypes.c_int, handle, handle)
        self._api(
            "QueryInformationJobObject", ctypes.c_int, handle, ctypes.c_int, pointer, dword, pointer
        )
        self._api("TerminateJobObject", ctypes.c_int, handle, ctypes.c_uint)
        self._api("TerminateProcess", ctypes.c_int, handle, ctypes.c_uint)
        self._api("GenerateConsoleCtrlEvent", ctypes.c_int, dword, dword)
        self._api("CreateToolhelp32Snapshot", handle, dword, dword)
        self._api("Thread32First", ctypes.c_int, handle, pointer)
        self._api("Thread32Next", ctypes.c_int, handle, pointer)
        self._api("OpenThread", handle, dword, ctypes.c_int, dword)
        self._api("ResumeThread", dword, handle)

    def _api(self, name: str, result, *arguments) -> None:
        function = getattr(self.kernel, name)
        function.restype, function.argtypes = result, arguments

    @contextmanager
    def process_handle(self, pid: int, rights: int = 0x101000):
        handle = self.kernel.OpenProcess(rights, False, pid)
        if not handle:
            code = ctypes.get_last_error()
            if code == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists.
                raise ProcessLookupError(pid)
            raise ctypes.WinError(code)
        try:
            yield handle
        finally:
            self.kernel.CloseHandle(handle)

    def _identity(self, pid: int, handle) -> ProcessIdentity:
        if self.kernel.WaitForSingleObject(handle, 0) != 258:  # WAIT_TIMEOUT
            raise ProcessLookupError(pid)
        created, exited, kernel, user = (ctypes.c_uint64() for _ in range(4))
        if not self.kernel.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return ProcessIdentity(pid, created.value)

    def identity(self, pid: int) -> ProcessIdentity:
        with self.process_handle(pid) as handle:
            return self._identity(pid, handle)

    @staticmethod
    def job_name(identity: ProcessIdentity) -> str:
        return f"Local\\agent-comms-{identity.pid}-{identity.start_time}"

    @contextmanager
    def job(self, identity: ProcessIdentity):
        handle = self.kernel.OpenJobObjectW(0x000C, False, self.job_name(identity))
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield handle
        finally:
            self.kernel.CloseHandle(handle)

    def group_members(self, leader: ProcessIdentity) -> tuple[ProcessIdentity, ...]:
        try:
            with self.job(leader) as job:
                capacity = 16
                while True:
                    # JOBOBJECT_BASIC_PROCESS_ID_LIST: two DWORDs then ULONG_PTRs.
                    buffer = ctypes.create_string_buffer(
                        8 + ctypes.sizeof(ctypes.c_size_t) * capacity
                    )
                    if self.kernel.QueryInformationJobObject(job, 3, buffer, len(buffer), None):
                        count = ctypes.c_uint32.from_buffer(buffer, 4).value
                        identifiers = (ctypes.c_size_t * count).from_buffer(buffer, 8)
                        members = []
                        for pid in identifiers:
                            with suppress(ProcessLookupError):
                                members.append(self.identity(pid))
                        return tuple(members)
                    if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                        raise ctypes.WinError(ctypes.get_last_error())
                    capacity *= 2
        except FileNotFoundError:
            if self.matches(leader):
                raise RuntimeError("Live process has no owned Windows job") from None
            return ()

    def send(self, identity: ProcessIdentity, signum: int) -> None:
        with self.process_handle(identity.pid, 0x101001) as handle:
            if self._identity(identity.pid, handle) != identity:
                raise IdentityMismatchError(identity.pid)
            if not self.kernel.TerminateProcess(handle, signum):
                raise ctypes.WinError(ctypes.get_last_error())

    def terminate_group(self, identity: ProcessIdentity) -> None:
        if not self.matches(identity):
            return  # Surviving job members are still forced in the common stop plan.
        # Console controls are best-effort: detached/non-console processes
        # need not have a console or a handler. The common grace deadline still
        # forces the exact named job; failure to force remains an error.
        self.kernel.GenerateConsoleCtrlEvent(1, identity.pid)  # CTRL_BREAK_EVENT

    def force_group(self, identity: ProcessIdentity) -> None:
        try:
            with self.job(identity) as job:
                if not self.kernel.TerminateJobObject(job, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
        except FileNotFoundError:
            if self.matches(identity):
                raise RuntimeError("Refusing to kill a process without its owned job") from None

    def bind_and_resume(self, identity: ProcessIdentity) -> None:
        with self.process_handle(identity.pid, 0x101101) as process:
            if self._identity(identity.pid, process) != identity:
                raise IdentityMismatchError(identity.pid)
            job = self.kernel.CreateJobObjectW(None, self.job_name(identity))
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self.kernel.AssignProcessToJobObject(job, process):
                    raise ctypes.WinError(ctypes.get_last_error())
                snapshot = self.kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
                if snapshot == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    entry = self.ThreadEntry()
                    entry.size = ctypes.sizeof(entry)
                    found = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
                    while found:
                        if entry.owner == identity.pid:
                            thread = self.kernel.OpenThread(2, False, entry.tid)
                            if not thread:
                                raise ctypes.WinError(ctypes.get_last_error())
                            try:
                                if self.kernel.ResumeThread(thread) == 0xFFFFFFFF:
                                    raise ctypes.WinError(ctypes.get_last_error())
                                return
                            finally:
                                self.kernel.CloseHandle(thread)
                        found = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
                    raise RuntimeError("Suspended child's primary thread is missing")
                finally:
                    self.kernel.CloseHandle(snapshot)
            except BaseException:
                self.kernel.TerminateProcess(process, 1)
                raise
            finally:
                # Named job stays alive while it contains processes. Detached
                # owners outlive this launcher; no kill-on-handle-close flag.
                self.kernel.CloseHandle(job)

    def launch(self, command: tuple[str, ...], pass_fds: tuple[int, ...]) -> ChildLaunch:
        if pass_fds:
            raise NotImplementedError("POSIX descriptor inheritance is unavailable on Windows")
        return WindowsLaunch(command, self)


class ChildLaunch(ABC):
    @property
    @abstractmethod
    def argv(self) -> tuple[str, ...]: ...

    @property
    @abstractmethod
    def options(self) -> dict: ...

    def spawn(
        self,
        *,
        cwd=None,
        env=None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) -> ParentedProcess:
        """Capture parent custody while the platform exec gate is still closed."""
        process = subprocess.Popen(
            self.argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            **self.options,
        )
        return ParentedProcess(process, Platform.current().identity(process.pid))

    @abstractmethod
    def release(self, identity: ProcessIdentity) -> None: ...

    @abstractmethod
    def cancel_before_release(self, identity: ProcessIdentity) -> None: ...

    @abstractmethod
    def verify(self) -> None: ...

    async def verify_async(self) -> None:
        self.verify()

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class WindowsLaunch(ChildLaunch):
    def __init__(self, command: tuple[str, ...], platform: Win32Platform):
        self.command, self.platform = command, platform

    @property
    def argv(self) -> tuple[str, ...]:
        return self.command

    @property
    def options(self) -> dict:
        # Job assignment precedes ResumeThread; arbitrary child code cannot
        # create an uncontained descendant between CreateProcess and binding.
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004}

    def release(self, identity: ProcessIdentity) -> None:
        try:
            self.platform.bind_and_resume(identity)
        except BaseException:
            self.cancel_before_release(identity)
            raise

    def cancel_before_release(self, identity: ProcessIdentity) -> None:
        # The executable is suspended: it cannot have created descendants.
        # No job may exist yet; signal its verified process handle directly.
        with suppress(ProcessLookupError):
            self.platform.send(identity, 1)

    def verify(self) -> None:
        pass  # CreateProcess itself reports launch failure before returning.

    def close(self) -> None:
        pass  # The kernel named job owns the detached lifetime.


class PosixLaunch(ChildLaunch):
    def __init__(self, command: tuple[str, ...], pass_fds: tuple[int, ...]):
        self.command = command
        self.pass_fds = pass_fds
        self.read_fd, self.write_fd = os.pipe()
        self.error_r, error_w = os.pipe()
        self.error_writer = os.fdopen(error_w, "wb", buffering=0)

    @property
    def argv(self) -> tuple[str, ...]:
        return (
            sys.executable,
            "-c",
            _EXEC_GATE,
            str(self.read_fd),
            str(self.error_writer.fileno()),
            *self.command,
        )

    @property
    def options(self) -> dict:
        return {
            "start_new_session": True,
            "pass_fds": (self.read_fd, self.error_writer.fileno(), *self.pass_fds),
        }

    def release(self, identity: ProcessIdentity) -> None:
        self.error_writer.close()
        os.write(self.write_fd, b"G")

    def cancel_before_release(self, identity: ProcessIdentity) -> None:
        # The exec gate has not released user code or spawned descendants.
        with suppress(ProcessLookupError):
            Platform.current().send(identity, signal.SIGKILL)

    def verify(self) -> None:
        error = os.read(self.error_r, 64)
        if error:
            code = int(error)
            raise OSError(code, os.strerror(code), self.command[0])

    async def verify_async(self) -> None:
        await _exec_error(self.error_r, self.command[0])

    def close(self) -> None:
        self.error_writer.close()
        for fd in (self.read_fd, self.write_fd, self.error_r):
            os.close(fd)


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


async def join_retirement(task: asyncio.Future):
    """Join owned cleanup through repeated cancellation, then propagate it."""
    interrupted = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            interrupted = error
    result = task.result()
    if interrupted is not None:
        raise interrupted
    return result


class ChildProcess(Sealed, ABC):
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

    def force(self) -> None:
        """Signal the original owned group, including an exited leader's members."""
        self.platform.force_group(self.identity)

    @property
    def retired(self) -> bool:
        """Exact child and its owned group are gone, independent of pipe callbacks."""
        return not self.alive() and not self.platform.group_members(self.identity)

    @abstractmethod
    async def wait(self) -> ChildOutcome: ...

    def _stop_plan(self, guard):
        with guard():
            self.platform.terminate_group(self.identity)
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        stage = GracefulStopOutcome
        while self.platform.group_members(self.identity):
            if time.monotonic() >= deadline:
                stage = ForcedStopOutcome
                with guard():
                    self.platform.force_group(self.identity)
                break
            yield 0.02
        while self.platform.group_members(self.identity):
            if time.monotonic() >= deadline + STOP_GRACE_SECONDS:
                raise RuntimeError("Child process group did not retire")
            yield 0.02
        return stage

    def _retire_group(self, guard):
        """Run the same guarded physical plan for synchronous and async custody."""
        plan = self._stop_plan(guard)
        while True:
            try:
                delay = next(plan)
            except StopIteration as done:
                return done.value
            time.sleep(delay)

    async def _stop(self) -> ChildOutcome:
        pending = asyncio.get_running_loop().run_in_executor(
            None, self._retire_group, nullcontext
        )
        stage = await join_retirement(pending)
        self._release_retired_io()
        async with asyncio.timeout(STOP_GRACE_SECONDS):
            return stage(await self.wait())

    def _release_retired_io(self) -> None:
        """Release owned IO after the identity-bound process group has retired."""
        return None

    async def stop(self) -> ChildOutcome:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        return await join_retirement(self._stop_task)


class ChildStdio(DeclaredFamily, affix="ChildStdio"):
    """Launch IO selection; process identity and retirement remain ChildProcess-owned."""

    @property
    @abstractmethod
    def options(self) -> dict: ...


@dataclass(frozen=True)
class StreamingChildStdio(ChildStdio):
    input_enabled: bool = True
    limit: int = 65536

    @property
    def options(self) -> dict:
        return {
            "stdin": subprocess.PIPE if self.input_enabled else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class TerminalChildStdio(ChildStdio):
    terminal: io.IOBase

    @property
    def options(self) -> dict:
        return {"stdin": self.terminal, "stdout": self.terminal, "stderr": self.terminal}


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
        stdio: ChildStdio = StreamingChildStdio(),
    ) -> AttachedChild:
        if not command:
            raise ValueError("A child command is required")
        platform = Platform.current()
        with platform.launch(command, pass_fds) as launch:
            process = await asyncio.create_subprocess_exec(
                *launch.argv,
                cwd=cwd,
                env=env,
                **stdio.options,
                **launch.options,
            )
            identity = platform.identity(process.pid)
            child = cls(process, identity)
            try:
                launch.release(identity)
                await launch.verify_async()
            except BaseException:
                await child.stop()
                raise
        return child

    async def write(self, data: bytes) -> None:
        """Write to this still-running child's input and observe transport backpressure."""
        if self.stdin is None or self.returncode is not None:
            raise BrokenPipeError
        self.stdin.write(data)
        await self.stdin.drain()

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    async def wait(self) -> ChildOutcome:
        return ChildOutcome.from_returncode(await self.process.wait())

    def _release_retired_io(self) -> None:
        # asyncio's wait also waits for pipe disconnects. A cancelled reader
        # can leave a full pipe paused forever, even after process exit. Close
        # the owned subprocess transport only after the group has retired;
        # don't decode, accumulate, or replay abandoned output to unblock wait.
        self.process._transport.close()

    async def discard_stderr(self) -> None:
        """Drain unwanted output through EOF without retaining the child's output.

        Chunk size controls read allocation only, never total accepted output.
        The caller owns cancellation together with the child session lifetime.
        """
        if self.stderr is not None:
            while await self.stderr.read(io.DEFAULT_BUFFER_SIZE):
                pass

    def close_input(self) -> None:
        if self.stdin is not None:
            with suppress(OSError):
                self.stdin.close()

    async def finish(self) -> ChildOutcome:
        """Allow a completed streaming protocol to flush and exit after EOF."""
        self.close_input()
        try:
            async with asyncio.timeout(STOP_GRACE_SECONDS):
                outcome = await self.wait()
            await self.stop()  # Also retire descendants of an exited leader.
            return outcome
        except TimeoutError:
            return TimedOutOutcome(await self.stop())


class ParentLifeline:
    """Inherited pipe for a Python child which must die with its launcher.

    Lifted from the recovery reader's parent watchdog. The child enters guard()
    before calling the blocking operation; EOF releases OS-owned locks even
    when the operation is stuck on another Python thread.
    """

    variable = "AGENT_COMMS_PARENT_LIFELINE_FD"

    def __enter__(self):
        self.read_fd, self.write_fd = os.pipe()
        return self

    def __exit__(self, *_):
        os.close(self.read_fd)
        os.close(self.write_fd)

    @property
    def environment(self) -> dict[str, str]:
        return {**os.environ, self.variable: str(self.read_fd)}

    @classmethod
    def guard(cls) -> None:
        descriptor = int(os.environ.pop(cls.variable))
        os.fstat(descriptor)

        def parent_watchdog() -> None:
            try:
                os.read(descriptor, 1)
            finally:
                os._exit(1)

        threading.Thread(target=parent_watchdog, daemon=True).start()


class InheritedDeadline:
    """Pidfd watchdog custody; preserves the direct parent's authority FDs."""

    def __init__(self, platform: PidfdHandles, child: ParentedProcess, deadline: float):
        self.platform, self.child, self.deadline = platform, child, deadline
        self.custody = ExitStack()

    def __enter__(self):
        try:
            descriptor = self.platform.open_pidfd(self.child.pid)
            self.custody.callback(os.close, descriptor)
            # Only the pidfd crosses to the watchdog, never authority FDs.
            with Platform.current().launch(
                WatchDeadlineCommand(descriptor, self.deadline).argv(), (descriptor,)
            ) as launch:
                watcher = self.custody.enter_context(launch.spawn(stdout=subprocess.PIPE))
                launch.release(watcher.identity)
                launch.verify()
            self.custody.callback(self.child.stop_sync)
            assert watcher.process.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(watcher.process.stdout, selectors.EVENT_READ)
                if not selector.select(max(0, self.deadline - time.monotonic())):
                    raise TimeoutError("Inherited child watchdog did not arm before deadline")
            if watcher.process.stdout.readline() != b"armed\n":
                raise RuntimeError("Inherited child watchdog failed to arm")
            if time.monotonic() >= self.deadline:
                raise TimeoutError("Inherited child deadline elapsed before exec")
            return self
        except BaseException:
            self.custody.close()
            raise

    def __exit__(self, *_):
        self.custody.close()


class BoundedRun:
    @staticmethod
    def require_inherited_deadline() -> PidfdHandles:
        """Probe the existing exact-process capability before committing intent."""
        platform = Platform.current()
        if not isinstance(platform, PidfdHandles):
            raise NotImplementedError("Inherited deadline requires Linux pidfd capability")
        try:
            descriptor = platform.open_pidfd(os.getpid())
            try:
                platform.signal_pidfd(descriptor, 0)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise NotImplementedError("Kernel pidfd deadline support unavailable") from error
        return platform

    @classmethod
    def run_inherited(
        cls,
        command: tuple[str, ...],
        *,
        deadline: float,
        pass_fds: tuple[int, ...],
        input: bytes | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ChildResult:
        """Bounded direct-parent execution retaining the caller's authority FDs.

        The trusted non-forking executable keeps the owner as its real parent.
        A sibling pidfd watchdog inherits only its pidfd, arms before exec, and
        survives owner death. The namespace shape is a distinct stronger tree
        capability and cannot preserve this external parent-PID proof.
        """
        platform = cls.require_inherited_deadline()
        if not command or not math.isfinite(deadline) or deadline <= time.monotonic():
            raise ValueError("A command and future finite absolute deadline are required")
        for descriptor in pass_fds:
            os.fstat(descriptor)
        with ExitStack() as custody, platform.launch(command, pass_fds) as launch:
            child = custody.enter_context(
                launch.spawn(
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
            try:
                # Watchdog lifetime is outside child retirement, even on
                # exceptions: the authority-bearing child retires first.
                with InheritedDeadline(platform, child, deadline):
                    launch.release(child.identity)
                    launch.verify()
                    stdout, stderr = child.process.communicate(
                        input, timeout=max(0, deadline - time.monotonic())
                    )
                    if time.monotonic() >= deadline:
                        return ChildResult(TimedOutOutcome(child.stop_sync()), stdout, stderr)
                    return ChildResult(child.reap(), stdout, stderr)
            except (TimeoutError, subprocess.TimeoutExpired):
                return ChildResult(TimedOutOutcome(child.stop_sync()))

    @classmethod
    @asynccontextmanager
    async def session(cls, command: tuple[str, ...], *, timeout: float, **options):
        """One bounded request/response exchange; caller owns its protocol."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("A positive finite timeout is required")
        child = None
        try:
            async with asyncio.timeout(timeout):
                child = await AttachedChild.start(command, **options)
                yield child
        finally:
            if child is not None:
                await child.stop()

    @classmethod
    async def run(
        cls,
        command: tuple[str, ...],
        *,
        timeout: float,
        input: bytes | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> ChildResult:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("A positive finite timeout is required")
        try:
            child = await AttachedChild.start(command, cwd=cwd, env=env, pass_fds=pass_fds)
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
            try:
                await child.stop()
            finally:
                await join_retirement(exchange)


class SynchronousProcess(ChildProcess):
    """Identity-bound control with synchronous reaping supplied by custody."""

    def require_stop_authority(self) -> None:
        """A retained parent handle already establishes stop/reap authority."""

    def force(self) -> None:
        self.platform.require(self.identity)
        super().force()

    async def stop(self) -> ChildOutcome:
        if self._stop_task is None:
            self.require_stop_authority()
        return await super().stop()

    @abstractmethod
    def reap(self) -> ChildOutcome: ...

    def stop_sync(self, *, guard=nullcontext) -> ChildOutcome:
        self.require_stop_authority()
        stage = self._retire_group(guard)
        return stage(self.reap())


class ObservedProcess(SynchronousProcess):
    """A recorded incarnation: may signal it, cannot claim a parent's exit code."""

    def require_stop_authority(self) -> None:
        self.platform.require(self.identity)

    def reap(self) -> ChildOutcome:
        return DetachedExitOutcome()

    async def wait(self) -> ChildOutcome:
        while self.alive():
            await asyncio.sleep(0.02)
        return self.reap()


class ParentedProcess(SynchronousProcess):
    """Owns the OS child handle, its reap result, and any captured pipe streams."""

    def __init__(self, process: subprocess.Popen[bytes], identity: ProcessIdentity):
        super().__init__(identity)
        self.process = process

    @classmethod
    def launch(
        cls,
        command: tuple[str, ...],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        output: Any = subprocess.DEVNULL,
        before_start: Callable[[ProcessIdentity], None] | None = None,
    ) -> ParentedProcess:
        if not command:
            raise ValueError("A child command is required")
        with Platform.current().launch(command, ()) as launch:
            child = launch.spawn(cwd=cwd, env=env, stdout=output, stderr=output)
            try:
                if before_start is not None:
                    before_start(child.identity)
            except BaseException:
                launch.cancel_before_release(child.identity)
                child.reap()
                child.close_streams()
                raise
            try:
                launch.release(child.identity)
                launch.verify()
            except BaseException:
                child.close()
                raise
        return child

    def reap(self) -> ChildOutcome:
        return ChildOutcome.from_returncode(self.process.wait(timeout=STOP_GRACE_SECONDS))

    async def wait(self) -> ChildOutcome:
        while self.process.poll() is None:
            await asyncio.sleep(0.02)
        return self.reap()

    def close_streams(self) -> None:
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()

    def close(self) -> None:
        try:
            self.stop_sync()
        finally:
            self.close_streams()

    def __enter__(self) -> ParentedProcess:
        return self

    def __exit__(self, *_):
        self.close()


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
            "-c",
            "from agent_comms.child_process import ChildCommand; ChildCommand.main()",
            json.dumps(FieldCodec.encode(self)),
        )

    @classmethod
    def main(cls) -> None:
        FieldCodec.decode(cls, json.loads(sys.argv[1])).run()

    @abstractmethod
    def run(self) -> None: ...


@dataclass(frozen=True)
class ControllingTerminalCommand(ChildCommand):
    """Acquire stdin's PTY after exec, inside the acquired child's new session."""

    command: tuple[str, ...]

    def run(self) -> None:
        import fcntl
        import termios

        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.execvpe(self.command[0], self.command, os.environ)


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
