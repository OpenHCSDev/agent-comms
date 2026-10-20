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
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator
from contextlib import (
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
    nullcontext,
    suppress,
)
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, NullMember, RecordMember, SubclassUnion
from .launch_runtime import LaunchRuntime
from .sealed import Sealed

if TYPE_CHECKING:
    from .registration_change import RegistrationChange, UpdatedRegistration
    from .runtime_children import RuntimeChildrenStore, RuntimeChildReservation
    from .store_files import LockMode
    from .thread_identity import ThreadIncarnation, ThreadRole
    from .thread_presentation import ThreadOwnerBinding, ThreadPresentation
    from .thread_status import ThreadStatus
    from .turn_lease import ActiveTurn

STOP_GRACE_SECONDS = 2.0


class IdentityMismatchError(ProcessLookupError):
    """A recorded process incarnation is absent or has been replaced."""


class ChildRetirementError(BaseException):
    """The child owner's cleanup acknowledgement failed independently of cancellation."""


class ChildRetirementFailed(RuntimeError, ChildRetirementError):
    """An ordinary retirement failure; never a cancellation."""


class ChildRetirementCancelled(asyncio.CancelledError, ChildRetirementError):
    """Preserve cancellation and an independently failed retirement acknowledgement."""


class ProcessState(SubclassUnion, affix=""):
    """An exact recorded process or a worker which has never started."""

    @abstractmethod
    def allows_owner_start(self) -> bool: ...
    @abstractmethod
    def allows_inactive_stop(self) -> bool: ...
    @abstractmethod
    def allows_goal_observation(self, status: ThreadStatus) -> bool: ...
    @abstractmethod
    def claims_owner(self, status: ThreadStatus) -> bool: ...

    @abstractmethod
    def require_held_by(self, role: type[ThreadRole]) -> None: ...
    @abstractmethod
    def conflicts_with(self, previous: ProcessState) -> bool: ...

    @abstractmethod
    def registration_change(
        self, previous: ProcessState, change: UpdatedRegistration, incarnation: ThreadIncarnation,
    ) -> RegistrationChange: ...

    @abstractmethod
    def restarted_registration(
        self, change: UpdatedRegistration, incarnation: ThreadIncarnation,
    ) -> RegistrationChange: ...

    @abstractmethod
    def native_presentation(
        self, ordinary: ThreadPresentation, binding: ThreadOwnerBinding,
    ) -> ThreadPresentation: ...

    @abstractmethod
    def attachment_process(self, previous: ProcessState) -> ProcessState: ...

    @abstractmethod
    def offline(self) -> bool: ...

    @abstractmethod
    def require(self, message: str = "No worker process has started") -> ProcessIdentity: ...

    @abstractmethod
    def alive(self) -> bool: ...

    @abstractmethod
    def runs_here(self) -> bool: ...
    @abstractmethod
    def owns_turn(self, turn: ActiveTurn) -> bool: ...

    @abstractmethod
    def signal(self, signum: int) -> None: ...

    @abstractmethod
    def require_release_by(self, process: ProcessIdentity) -> None: ...

    @abstractmethod
    def pid_fields(self) -> dict[str, int]: ...


@dataclass(frozen=True)
class NoProcess(NullMember, ProcessState):
    def allows_owner_start(self) -> bool:
        return True

    def allows_inactive_stop(self) -> bool:
        return True

    def allows_goal_observation(self, status: ThreadStatus) -> bool:
        return False

    def claims_owner(self, status: ThreadStatus) -> bool:
        return False

    def require_held_by(self, role: type[ThreadRole]) -> None:
        """No process is held, so any role may declare it."""

    def conflicts_with(self, previous: ProcessState) -> bool:
        return False

    def registration_change(
        self, previous: ProcessState, change: UpdatedRegistration, incarnation: ThreadIncarnation,
    ) -> RegistrationChange:
        return change

    def restarted_registration(
        self, change: UpdatedRegistration, incarnation: ThreadIncarnation,
    ) -> RegistrationChange:
        return change

    def native_presentation(
        self, ordinary: ThreadPresentation, binding: ThreadOwnerBinding,
    ) -> ThreadPresentation:
        return ordinary

    def attachment_process(self, previous: ProcessState) -> ProcessState:
        return previous

    def offline(self) -> bool:
        return False

    def require(self, message: str = "No worker process has started") -> ProcessIdentity:
        raise RelationViolationError(message)

    def alive(self) -> bool:
        return False

    def runs_here(self) -> bool:
        return False

    def owns_turn(self, turn: ActiveTurn) -> bool:
        return False

    def signal(self, signum: int) -> None:
        pass

    def require_release_by(self, process: ProcessIdentity) -> None:
        # Stopping before the first start still produces a valid release.
        pass

    def pid_fields(self) -> dict[str, int]:
        return {}


@dataclass(frozen=True)
class ProcessIdentity(RecordMember, ProcessState):
    pid: int
    start_time: int

    def __post_init__(self) -> None:
        if self.pid <= 0 or self.start_time <= 0:
            raise ValueError("A positive PID and platform start time are required")

    @classmethod
    def pid_argument(cls, value: str) -> dict[str, object]:
        return FieldCodec.encode_record(cls.capture(int(value)))

    @classmethod
    def capture(cls, pid: int) -> ProcessIdentity:
        return Platform.current().identity(pid)

    def allows_owner_start(self) -> bool:
        return False

    def allows_inactive_stop(self) -> bool:
        return self.runs_here()

    def allows_goal_observation(self, status: ThreadStatus) -> bool:
        return status.active

    def claims_owner(self, status: ThreadStatus) -> bool:
        return status.active

    def require_held_by(self, role: type[ThreadRole]) -> None:
        role.require_executable(
            RelationViolationError("A human thread cannot hold an owner process")
        )

    def conflicts_with(self, previous: ProcessState) -> bool:
        return self != previous

    def offline(self) -> bool:
        return not self.alive()

    def native_presentation(
        self, ordinary: ThreadPresentation, binding: ThreadOwnerBinding,
    ) -> ThreadPresentation:
        return binding.native_presentation(ordinary)

    def attachment_process(self, previous: ProcessState) -> ProcessState:
        return self

    def require(self, message: str = "No worker process has started") -> ProcessIdentity:
        return self

    @staticmethod
    def in_group(pid: int, start_time: int, pgid: int) -> ProcessIdentity:
        return GroupLeader(pid, start_time) if pgid == pid else GroupMember(pid, start_time)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProcessIdentity):
            return NotImplemented
        return (self.pid, self.start_time) == (other.pid, other.start_time)

    def alive(self) -> bool:
        return Platform.current().matches(self)

    def runs_here(self) -> bool:
        return os.getpid() == self.pid

    def owns_turn(self, turn: ActiveTurn) -> bool:
        return turn.owner_pid == self.pid

    def signal(self, signum: int) -> None:
        Platform.current().send(self, signum)

    def require_release_by(self, process: ProcessIdentity) -> None:
        if self != process:
            raise RelationViolationError("Only the registered owner may release itself.")

    def pid_fields(self) -> dict[str, int]:
        return {"pid": self.pid}

    def registration_change(
        self, previous: ProcessState, change: UpdatedRegistration, incarnation: ThreadIncarnation,
    ) -> RegistrationChange:
        return previous.restarted_registration(change, incarnation)

    def restarted_registration(
        self, change: UpdatedRegistration, incarnation: ThreadIncarnation,
    ) -> RegistrationChange:
        return change.restarted() if incarnation != change.previous.incarnation else change

    def members(self, platform: PosixPlatform) -> tuple[ProcessIdentity, ...]:
        return ()  # A stored, exited identity cannot grant group ownership.


NO_PROCESS = NoProcess()


class GroupLeader(ProcessIdentity, declared_name=None):
    def members(self, platform: PosixPlatform) -> tuple[ProcessIdentity, ...]:
        return tuple(member for member in platform.members_of_group(self.pid)
                     if member.start_time >= self.start_time)


class GroupMember(ProcessIdentity, declared_name=None):
    def members(self, platform: PosixPlatform) -> tuple[ProcessIdentity, ...]:
        return (self,) if platform.matches(self) else ()


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
        return cls.member_named(sys.platform)()

    @abstractmethod
    def try_store_lock(self, descriptor: int, *, mode: LockMode) -> None:
        """One nonblocking native attempt; busy is BlockingIOError."""

    def acquire_store_lock(self, descriptor: int, *, mode: LockMode, blocking: bool) -> None:
        """Use the common physical wait driver when the platform needs polling."""
        from .store_files import StoreLockContention

        StoreLockContention(math.inf if blocking else 0).acquire(descriptor, self, mode=mode)

    def release_store_lock(self, descriptor: int) -> None:
        """POSIX locks end at last close, including inherited descriptors."""

    @abstractmethod
    def launch(
        self, command: tuple[str, ...], pass_fds: tuple[int, ...], runtime: LaunchRuntime
    ) -> ChildLaunch: ...

    def terminate_group(self, identity: ProcessIdentity) -> None:
        self.signal_group(identity, signal.SIGTERM)

    def force_group(self, identity: ProcessIdentity) -> None:
        self.signal_group(identity, signal.SIGKILL)

    @abstractmethod
    def identity(self, pid: int) -> ProcessIdentity: ...

    def matches(self, identity: ProcessIdentity) -> bool:
        try:
            return self.identity(identity.pid) == identity
        except ProcessLookupError:
            return False

    def require(self, identity: ProcessIdentity) -> ProcessIdentity:
        with suppress(ProcessLookupError):
            current = self.identity(identity.pid)
            if current == identity:
                return current
        raise IdentityMismatchError(f"Process incarnation changed: {identity.pid}")

    @abstractmethod
    def send(self, identity: ProcessIdentity, signum: int) -> None: ...

    @abstractmethod
    def group_members(self, leader: ProcessIdentity) -> tuple[ProcessIdentity, ...]: ...

    def signal_group(self, leader: ProcessIdentity, signum: int) -> None:
        # Snapshot identities, then signal each exact member. Never signal a
        # numeric group after its leader may have been reaped and reused.
        signalled = 0
        for member in self.group_members(leader):
            try:
                self.send(member, signum)
            except ProcessLookupError:
                continue
            signalled += 1
        if not signalled and self.matches(leader):
            raise RuntimeError(f"Live process {leader.pid} was not signalled")


class PosixPlatform(Platform):
    def acquire_store_lock(self, descriptor: int, *, mode: LockMode, blocking: bool) -> None:
        import fcntl

        fcntl.flock(descriptor, mode.flock() | (0 if blocking else fcntl.LOCK_NB))

    def try_store_lock(self, descriptor: int, *, mode: LockMode) -> None:
        import fcntl

        fcntl.flock(descriptor, mode.flock() | fcntl.LOCK_NB)

    def launch(
        self, command: tuple[str, ...], pass_fds: tuple[int, ...], runtime: LaunchRuntime
    ) -> ChildLaunch:
        return PosixLaunch(command, pass_fds, runtime)

    def group_members(self, leader: ProcessIdentity) -> tuple[ProcessIdentity, ...]:
        try:
            current = self.identity(leader.pid)
        except ProcessLookupError:
            return leader.members(self)
        if current != leader:
            raise IdentityMismatchError(f"Process incarnation changed: {leader.pid}")
        return current.members(self)

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
        group = int(fields[2])
        return ProcessIdentity.in_group(pid, int(fields[19]), group), group

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
        return ProcessIdentity.in_group(
            pid, info.start_seconds * 1_000_000 + info.start_microseconds, info.pgid,
        )

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


class Win32Platform(Platform):
    """Kernel creation times and named job objects bind the complete child tree."""

    store_lock_interval = 0.01

    def try_store_lock(self, descriptor: int, *, mode: LockMode) -> None:
        import errno
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            # The Windows runtime has no shared lock: LK_NBRLCK is LK_NBLCK.
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
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

    def launch(
        self, command: tuple[str, ...], pass_fds: tuple[int, ...], runtime: LaunchRuntime
    ) -> ChildLaunch:
        if pass_fds:
            raise NotImplementedError("POSIX descriptor inheritance is unavailable on Windows")
        return WindowsLaunch(command, self, runtime)


class ChildRegistration(ABC):
    """The durable owner notified after a launched group has finished."""

    @abstractmethod
    def finished(self, identity: ProcessIdentity) -> None: ...


class NoChildRegistration(ChildRegistration):
    """An observed process has no launch registration to remove."""

    def finished(self, identity: ProcessIdentity) -> None:
        pass


NO_CHILD_REGISTRATION = NoChildRegistration()


class ChildLaunch(ABC):
    # The composition root binds the store after process types are declared.
    children_store: ClassVar[type[RuntimeChildrenStore]]

    def __init__(self, runtime: LaunchRuntime):
        self.runtime = runtime

    def record(self, identity: ProcessIdentity, reservation: RuntimeChildReservation) -> None:
        """Persist the gated child, or cancel before user code executes."""
        try:
            reservation.record(identity)
        except BaseException:
            self.cancel_before_release(identity)
            raise

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
        """Record the parent's process handle before the platform allows exec."""
        with self.children_store.launching(self.runtime) as reservation:
            process = subprocess.Popen(
                self.argv, cwd=cwd, env=env, stdin=stdin, stdout=stdout,
                stderr=stderr, **self.options,
            )
            identity = Platform.current().identity(process.pid)
            child = ParentedProcess(process, identity, reservation.store)
            try:
                self.record(identity, reservation)
            except BaseException:
                child.reap()
                child.close_streams()
                raise
        return child

    async def spawn_async(self, *, cwd, env, stdio: ChildStdio) -> AttachedChild:
        async with self.children_store.launching_async(self.runtime) as reservation:
            process = await asyncio.create_subprocess_exec(
                self.argv[0], *self.argv[1:], cwd=cwd, env=env, **stdio.options, **self.options,
            )
            identity = Platform.current().identity(process.pid)
            child = AttachedChild(process, identity, reservation.store)
            child._stderr_task = asyncio.create_task(child._drain_stderr())
            try:
                self.record(identity, reservation)
            except BaseException:
                await process.wait()
                await child._stderr_task
                raise
        return child

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
    def __init__(self, command: tuple[str, ...], platform: Win32Platform, runtime: LaunchRuntime):
        super().__init__(runtime)
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
    def __init__(self, command: tuple[str, ...], pass_fds: tuple[int, ...], runtime: LaunchRuntime):
        super().__init__(runtime)
        self.command = command
        self.pass_fds = pass_fds
        self.read_fd, self.write_fd = os.pipe()
        self.error_r, error_w = os.pipe()
        self.error_writer = os.fdopen(error_w, "wb", buffering=0)

    @property
    def argv(self) -> tuple[str, ...]:
        # The exec gate runs this decoder's code before exec of the selected command.
        return (
            str(LaunchRuntime.running().interpreter),
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


class CheckPrinted(DeclaredFamily, affix="Printed"):
    """The count printed by a child, or the absence of an integer."""

    @staticmethod
    def decoded(stdout: bytes) -> CheckPrinted:
        counts = re.findall(r"\d+", stdout.decode(errors="replace"))
        return CountPrinted(int(counts[-1])) if counts else NoIntegerPrinted()

    @abstractmethod
    def unproven(self, command: str, expected: int) -> tuple[str, ...]: ...

    @abstractmethod
    def distance(self, expected: int) -> CheckPrinted: ...

    @abstractmethod
    def plus(self, amount: int) -> CheckPrinted: ...

    @abstractmethod
    def require(self, expected: object) -> None: ...

    @abstractmethod
    def project[T](self, counted: Callable[[int], T],
                   uncounted: Callable[[NoIntegerPrinted], T]) -> T: ...


@dataclass(frozen=True)
class CountPrinted(CheckPrinted):
    count: int

    def distance(self, expected: int) -> CheckPrinted:
        return CountPrinted(abs(self.count - expected))

    def plus(self, amount: int) -> CheckPrinted:
        return CountPrinted(self.count + amount)

    def require(self, expected: object) -> None:
        if self.count:
            raise ValueError(f"The order check is {self.count} away from {expected}.")

    def project[T](self, counted: Callable[[int], T],
                   uncounted: Callable[[NoIntegerPrinted], T]) -> T:
        return counted(self.count)

    def unproven(self, command: str, expected: int) -> tuple[str, ...]:
        return ((f"Check already prints {self.count} before any work: `{command}` proves nothing; "
                 "write a check that fails now",) if self.count == expected else ())


@dataclass(frozen=True)
class NoIntegerPrinted(CheckPrinted):
    def distance(self, expected: int) -> CheckPrinted:
        return self

    def plus(self, amount: int) -> CheckPrinted:
        return self

    def require(self, expected: object) -> None:
        raise ValueError(f"The order check cannot prove {expected}: it printed no integer.")

    def project[T](self, counted: Callable[[int], T],
                   uncounted: Callable[[NoIntegerPrinted], T]) -> T:
        return uncounted(self)

    def progress(self, command: str) -> str:
        return f"; check prints no integer: {command}"

    def unproven(self, command: str, expected: int) -> tuple[str, ...]:
        return ("the check printed no integer; use grep -c PATTERN FILE or ... | wc -l",)


class NoCountYetPrinted(NoIntegerPrinted):
    """An unfinished child contributes no count, including digits in partial stdout."""

    def require(self, expected: object) -> None:
        raise ValueError(f"The order check has no count yet; cannot prove {expected}.")

    def unproven(self, command: str, expected: int) -> tuple[str, ...]:
        return (f"the check has no count yet: `{command}` did not finish",)

    def progress(self, command: str) -> str:
        return "; no count yet"


class ChildOutcome(DeclaredFamily, affix="Outcome"):
    def printed_count(self, stdout: bytes) -> CheckPrinted:
        return NoCountYetPrinted()

    @property
    @abstractmethod
    def summary(self) -> str: ...

    @property
    @abstractmethod
    def successful(self) -> bool: ...

    @classmethod
    def from_returncode(cls, code: int) -> ChildOutcome:
        return SignaledOutcome(-code) if code < 0 else ExitedOutcome(code)


@dataclass(frozen=True)
class ExitedOutcome(ChildOutcome):
    code: int

    def printed_count(self, stdout: bytes) -> CheckPrinted:
        return CheckPrinted.decoded(stdout)

    @property
    def summary(self) -> str:
        return f"exited with code {self.code}"

    @property
    def successful(self) -> bool:
        return self.code == 0


@dataclass(frozen=True)
class SignaledOutcome(ChildOutcome):
    signal_number: int

    @property
    def summary(self) -> str:
        return f"stopped by {signal.Signals(self.signal_number).name}"

    @property
    def successful(self) -> bool:
        return False


@dataclass(frozen=True)
class TimedOutOutcome(ChildOutcome):
    termination: ChildOutcome

    @property
    def summary(self) -> str:
        return f"timed out; {self.termination.summary}"

    @property
    def successful(self) -> bool:
        return False


@dataclass(frozen=True)
class GracefulStopOutcome(ChildOutcome):
    termination: ChildOutcome

    @property
    def summary(self) -> str:
        return self.termination.summary

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
    def summary(self) -> str:
        return f"failed to start: {self.error}"

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
    try:
        while not task.done():
            try:
                # wait does not cancel the original future or replace its own
                # typed cancellation with shield's synthetic CancelledError.
                await asyncio.wait((task,))
            except asyncio.CancelledError as error:
                interrupted = error
        result = task.result()
    except BaseException as error:
        # A failed joined operation has still finished. Its error must
        # not consume the caller's cancellation and restart an observer loop.
        if interrupted is not None:
            raise interrupted from error
        raise
    if interrupted is not None:
        raise interrupted
    return result


class ChildProcess(Sealed, ABC):
    """The sole stop algorithm; cancellation retains the cleanup task."""

    def __init__(
        self, identity: ProcessIdentity, registration: ChildRegistration = NO_CHILD_REGISTRATION,
    ):
        self.registration = registration
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

    def _stop_plan(
        self, guard: Callable[[], AbstractContextManager[None]], deadline: float = math.inf,
    ) -> Generator[float, None, type[GracefulStopOutcome]]:
        with guard():
            self.platform.terminate_group(self.identity)
        grace_deadline = min(time.monotonic() + STOP_GRACE_SECONDS, deadline)
        stage = GracefulStopOutcome
        while self.platform.group_members(self.identity):
            if time.monotonic() >= grace_deadline:
                stage = ForcedStopOutcome
                with guard():
                    self.platform.force_group(self.identity)
                break
            yield 0.02
        # A killed process exits once the kernel releases it (swapped memory,
        # pending disk IO), which has no deadline. Kill again each round so a
        # member forked during the first kill retires too.
        while self.platform.group_members(self.identity):
            if time.monotonic() >= deadline:
                survivors = self.platform.group_members(self.identity)
                raise ChildRetirementFailed(f"Process group still alive: {survivors}")
            if stage is ForcedStopOutcome:
                with guard():
                    self.platform.force_group(self.identity)
            yield 0.02
        self.registration.finished(self.identity)
        return stage

    def _retire_group(self, guard, deadline=math.inf):
        """Use the same guarded stop plan for synchronous and asynchronous cleanup."""
        plan = self._stop_plan(guard, deadline)
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
        try:
            return await join_retirement(self._stop_task)
        except asyncio.CancelledError:
            # join_retirement completed the owned task before propagating cancellation.
            # Its original result, not the caller's exception, owns cleanup certainty.
            try:
                self._stop_task.result()
            except BaseException as error:
                raise ChildRetirementCancelled("Child retirement was not attested") from error
            raise
        except (OSError, RuntimeError) as error:
            raise ChildRetirementFailed("Child retirement was not attested") from error


class ChildStdio(DeclaredFamily, affix="ChildStdio"):
    """Launch IO selection; process identity and retirement remain ChildProcess-owned."""

    @property
    @abstractmethod
    def options(self) -> dict: ...


@dataclass(frozen=True)
class StreamingChildStdio(ChildStdio):
    input_enabled: bool = True
    limit: int = 65536
    stderr: int | io.IOBase = subprocess.PIPE

    @property
    def options(self) -> dict:
        return {
            "stdin": subprocess.PIPE if self.input_enabled else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": self.stderr,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class TerminalChildStdio(ChildStdio):
    terminal: io.IOBase

    @property
    def options(self) -> dict:
        return {"stdin": self.terminal, "stdout": self.terminal, "stderr": self.terminal}


class AttachedChild(ChildProcess):
    stderr_limit: ClassVar[int] = 16000

    def __init__(self, process: asyncio.subprocess.Process, identity: ProcessIdentity,
                 registration: ChildRegistration = NO_CHILD_REGISTRATION):
        super().__init__(identity, registration)
        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self._stderr_task: asyncio.Task[bytes]

    @classmethod
    async def start(
        cls,
        command: tuple[str, ...],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        pass_fds: tuple[int, ...] = (),
        stdio: ChildStdio = StreamingChildStdio(),
        runtime: LaunchRuntime,
    ) -> AttachedChild:
        if not command:
            raise ValueError("A child command is required")
        platform = Platform.current()
        with platform.launch(command, pass_fds, runtime) as launch:
            child = await launch.spawn_async(cwd=cwd, env=env, stdio=stdio)
            try:
                launch.release(child.identity)
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
        outcome = ChildOutcome.from_returncode(await self.process.wait())
        await asyncio.shield(self._stderr_task)
        return outcome

    def _release_retired_io(self) -> None:
        # asyncio's wait also waits for pipe disconnects. A cancelled reader
        # can leave a full pipe paused forever, even after process exit. Close
        # the owned subprocess transport only after the group has retired;
        # don't decode, accumulate, or replay abandoned output to unblock wait.
        self.process._transport.close()

    async def _drain_stderr(self) -> bytes:
        """The one pipe reader, started before releasing the child's exec gate."""
        tail = b""
        if self.stderr is not None:
            while chunk := await self.stderr.read(4096):
                tail = (tail + chunk)[-self.stderr_limit:]
        return tail

    async def stderr_tail(self, max_bytes: int = stderr_limit) -> str:
        return (
            (await asyncio.shield(self._stderr_task))[-max_bytes:].decode(errors="replace").strip()
        )

    async def _collect_stdout(self) -> bytes:
        output = bytearray()
        if self.stdout is not None:
            while chunk := await self.stdout.read(65536):
                output.extend(chunk)
        return bytes(output)

    async def communicate(self, data: bytes | None) -> tuple[bytes, bytes]:
        """Collect stdout without competing with the owned bounded stderr reader."""
        output = asyncio.create_task(self._collect_stdout())
        try:
            try:
                if data:
                    await self.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # A command may finish before consuming all supplied input.
            finally:
                self.close_input()
            return await output, await asyncio.shield(self._stderr_task)
        finally:
            if not output.done():
                output.cancel()
            await asyncio.gather(output, return_exceptions=True)

    def close_input(self) -> None:
        if self.stdin is not None:
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


class BoundedRun:
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
    async def run(cls, command: tuple[str, ...], *, timeout: float, **options) -> ChildResult:
        """Collect a result and join retirement before returning it."""
        async with cls.exchange(command, timeout=timeout, **options) as result:
            return result

    @classmethod
    @asynccontextmanager
    async def exchange(
        cls,
        command: tuple[str, ...],
        *,
        timeout: float,
        input: bytes | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        pass_fds: tuple[int, ...] = (),
        runtime: LaunchRuntime,
    ):
        """Borrow a collected result; this scope still owns child retirement.

        Launch and collection consume one absolute deadline. Consumers may deliver
        a verified response before group cleanup completes. Leaving the scope always
        joins that cleanup, including under cancellation.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("A positive finite timeout is required")
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            async with asyncio.timeout_at(deadline):
                child = await AttachedChild.start(
                    command, cwd=cwd, env=env, pass_fds=pass_fds, runtime=runtime
                )
        except TimeoutError:
            raise  # A launch expiry is not evidence that user code never started.
        except FileNotFoundError as error:
            yield ChildResult(FailedToStartOutcome(str(error)))
            return
        exchange = asyncio.create_task(child.communicate(input))
        try:
            try:
                async with asyncio.timeout_at(deadline):
                    stdout, stderr = await asyncio.shield(exchange)
                    outcome = await child.wait()
            except TimeoutError:
                outcome = TimedOutOutcome(await child.stop())
                stdout, stderr = await exchange
            yield ChildResult(outcome, stdout, stderr)
        finally:
            try:
                await child.stop()
            finally:
                await join_retirement(exchange)


class SynchronousProcess(ChildProcess):
    """Identity-checked process control; each subclass supplies synchronous reaping."""

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

    def stop_sync(self, *, guard=nullcontext, deadline=math.inf) -> ChildOutcome:
        self.require_stop_authority()
        stage = self._retire_group(guard, deadline)
        return stage(self.reap())


class ObservedProcess(SynchronousProcess):
    """A recorded incarnation: may signal it, cannot claim a parent's exit code."""

    def require_stop_authority(self) -> None:
        self.identity = self.platform.require(self.identity)

    def reap(self) -> ChildOutcome:
        return DetachedExitOutcome()

    async def wait(self) -> ChildOutcome:
        while self.alive():
            await asyncio.sleep(0.02)
        return self.reap()


class RecordedRuntimeGroup(ObservedProcess):
    """A recorded launch owns the group even after its original leader exits."""

    def require_stop_authority(self) -> None:
        self.platform.group_members(self.identity)


class ParentedProcess(SynchronousProcess):
    """Owns the OS child handle, its reap result, and any captured pipe streams."""

    def __init__(self, process: subprocess.Popen[bytes], identity: ProcessIdentity,
                 registration: ChildRegistration = NO_CHILD_REGISTRATION):
        super().__init__(identity, registration)
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
        runtime: LaunchRuntime,
        pass_fds: tuple[int, ...] = (),
    ) -> ParentedProcess:
        if not command:
            raise ValueError("A child command is required")
        with Platform.current().launch(command, pass_fds, runtime) as launch:
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
    def summary(self) -> str:
        return "exited; status unavailable"

    @property
    def successful(self) -> bool:
        return False


class NamespacedChild(ChildProcess):
    """Attached shape with kernel containment and an independent hard deadline.

    Lifted from the selected-Pi guardian: namespace PID1 attests readiness before
    exec release, a pidfd watchdog arms before launch, and killing unshare kills
    namespace PID1 (including descendants which escaped the process group).
    Business input/UNKNOWN journals stay with the caller.
    """

    def __init__(self, child: AttachedChild, watchdog: AttachedChild, namespace: str):
        super().__init__(child.identity)
        self.child = child
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
                pass_fds=(ready_w, release_r), runtime=LaunchRuntime.running(),
            )
            descriptor = platform.open_pidfd(child.pid)
            watchdog = await AttachedChild.start(
                WatchDeadlineCommand(descriptor, deadline).argv(),
                pass_fds=(descriptor,), runtime=LaunchRuntime.running(),
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

    async def wait(self) -> ChildOutcome:
        return await self.child.wait()

    async def _stop(self) -> ChildOutcome:
        result = await self.child.stop()
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
        # Decode this process's private command with the same declarations that encoded it.
        return (
            str(LaunchRuntime.running().interpreter),
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
