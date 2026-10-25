"""Identity-bound child ownership, process-group retirement and bounded runs.

Linux pidfd operations are lifted from compaction_child_watchdog. Process
identity is captured before releasing the exec gate, including for short jobs.
"""

from __future__ import annotations

import asyncio
import ctypes
import fcntl
import io
import json
import math
import multiprocessing
import os
import re
import select
import signal
import stat
import subprocess
import sys
import termios
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterator, Mapping
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import (
    AbstractContextManager,
    ExitStack,
    asynccontextmanager,
    contextmanager,
    nullcontext,
    suppress,
)
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, BinaryIO, ClassVar, NoReturn, TextIO

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, Id128, NullMember, RecordMember, SubclassUnion
from .launch_runtime import INHERITED_ENVIRONMENT, LaunchRuntime
from .sealed import Sealed

if TYPE_CHECKING:
    from .registration_change import RegistrationChange, UpdatedRegistration
    from .runtime_children import RuntimeChildrenStore, RuntimeChildReservation
    from .store_files import LockMode
    from .thread_identity import ThreadIncarnation, ThreadRole
    from .thread_presentation import ThreadOwnerBinding, ThreadPresentation
    from .thread_status import ActiveThreadPresence, ThreadStatus
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
    def thread_status(self, presence: ActiveThreadPresence, last_seen: float) -> ThreadStatus: ...

    @abstractmethod
    def runs_here(self) -> bool: ...
    @abstractmethod
    def owns_turn(self, turn: ActiveTurn) -> bool: ...

    @abstractmethod
    def signal(self, signum: int) -> None: ...

    @abstractmethod
    def require_release_by(self, process: ProcessIdentity) -> None: ...


@dataclass(frozen=True)
class NoProcess(NullMember, ProcessState):
    def allows_owner_start(self) -> bool:
        return True

    def allows_inactive_stop(self) -> bool:
        return True

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

    def thread_status(self, presence: ActiveThreadPresence, last_seen: float) -> ThreadStatus:
        return presence.heartbeat_seen(last_seen)

    def runs_here(self) -> bool:
        return False

    def owns_turn(self, turn: ActiveTurn) -> bool:
        return False

    def signal(self, signum: int) -> None:
        pass

    def require_release_by(self, process: ProcessIdentity) -> None:
        # Stopping before the first start still produces a valid release.
        pass


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

    def arguments(self) -> tuple[str, ...]:
        return Platform.current().arguments(self)

    def environment(self) -> dict[str, str]:
        return Platform.current().environment(self)

    def require_alive(self) -> ProcessIdentity:
        """Validate this recorded PID and start time against the live process."""
        return Platform.current().require(self)

    def runs_here(self) -> bool:
        return os.getpid() == self.pid

    def owns_turn(self, turn: ActiveTurn) -> bool:
        return turn.owner == self

    def signal(self, signum: int) -> None:
        Platform.current().send(self, signum)

    def lineage(self) -> Iterator[ProcessIdentity]:
        """This incarnation and its exact ancestors, ending at PID 1."""
        platform = Platform.current()
        current = self
        while True:
            yield current
            if current.pid == 1:
                return
            current = platform.parent_identity(current.pid)

    def require_ancestor_of(self, pid: int) -> None:
        """A supplied process is authority only for its actual descendant."""
        platform = Platform.current()
        platform.require(self)
        if self in platform.identity(pid).lineage():
            return
        raise IdentityMismatchError('The declared process is not an ancestor of this hook')

    async def wait_exited(self) -> None:
        await Platform.current().wait_exited(self)

    def require_release_by(self, process: ProcessIdentity) -> None:
        if self != process:
            raise RelationViolationError("Only the registered owner may release itself.")

    def thread_status(self, presence: ActiveThreadPresence, last_seen: float) -> ThreadStatus:
        return presence.process_seen(self)

    def registration_change(
        self, previous: ProcessState, change: UpdatedRegistration, incarnation: ThreadIncarnation,
    ) -> RegistrationChange:
        return previous.restarted_registration(change, incarnation)

    def restarted_registration(
        self, change: UpdatedRegistration, incarnation: ThreadIncarnation,
    ) -> RegistrationChange:
        return change.restarted() if incarnation != change.previous.incarnation else change

    def members(self, platform: Platform) -> tuple[ProcessIdentity, ...]:
        return ()  # A stored, exited identity cannot grant group ownership.


NO_PROCESS = NoProcess()


class GroupLeader(ProcessIdentity, declared_name=None):
    def members(self, platform: Platform) -> tuple[ProcessIdentity, ...]:
        return tuple(member for member in platform.members_of_group(self.pid)
                     if member.start_time >= self.start_time)


class GroupMember(ProcessIdentity, declared_name=None):
    def members(self, platform: Platform) -> tuple[ProcessIdentity, ...]:
        return (self,) if platform.matches(self) else ()


class LockHolder(DeclaredFamily, affix="LockHolder"):
    """An observed flock owner; diagnosis, not authority to signal it."""

    @abstractmethod
    def describe(self) -> str: ...


@dataclass(frozen=True, slots=True)
class IdentifiedLockHolder(LockHolder):
    pid: int

    def describe(self) -> str:
        return f"PID {self.pid}: {self.detail()}"

    @abstractmethod
    def detail(self) -> str: ...


@dataclass(frozen=True, slots=True)
class RunningLockHolder(IdentifiedLockHolder):
    command: str

    def detail(self) -> str:
        return self.command


class ExitedLockHolder(IdentifiedLockHolder):
    def detail(self) -> str:
        return "process exited"


class UnidentifiedLockHolder(LockHolder):
    def describe(self) -> str:
        return "kernel did not identify the owner"


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

class Platform(DeclaredFamily, affix="Platform"):
    store_lock_interval = 0.025

    @classmethod
    def current(cls) -> Platform:
        return cls.member_named(sys.platform)()

    def sync_file(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def sync_directory(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @abstractmethod
    def require_watched_procfs(self, refusal: Exception) -> None: ...

    def is_private(self, info: os.stat_result, permissions: int) -> bool:
        return stat.S_IMODE(info.st_mode) == permissions and self.is_owned(info)

    def is_owned(self, info: os.stat_result) -> bool:
        return info.st_uid == os.geteuid()

    def make_private(self, path: Path, permissions: int) -> None:
        with suppress(FileNotFoundError):
            if stat.S_IMODE(path.lstat().st_mode) != permissions:
                os.chmod(path, permissions, follow_symlinks=False)

    def make_private_descriptor(self, descriptor: int, permissions: int) -> None:
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != permissions:
            os.fchmod(descriptor, permissions)

    @contextmanager
    def socket_address(self, path: Path, descriptor: int) -> Generator[Path, None, None]:
        with TemporaryDirectory(prefix="ac-socket-", dir="/tmp") as directory:
            parent = Path(directory) / "d"
            parent.symlink_to(path.parent, target_is_directory=True)
            if not os.path.samestat(os.stat(parent), os.fstat(descriptor)):
                raise ValueError("Socket directory changed during address acquisition")
            yield parent / path.name

    def try_store_lock(self, descriptor: int, *, mode: LockMode) -> None:
        fcntl.flock(descriptor, mode.flock() | fcntl.LOCK_NB)

    def acquire_session_writer(self, descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)

    def launch(
        self, command: tuple[str, ...], pass_fds: tuple[int, ...], runtime: LaunchRuntime,
        lifetime: type[ChildLifetime],
    ) -> ChildLaunch:
        return ChildLaunch(self, command, pass_fds, runtime, lifetime)

    def terminate_group(self, identity: ProcessIdentity) -> None:
        self.signal_group(identity, signal.SIGTERM)

    def force_group(self, identity: ProcessIdentity) -> None:
        self.signal_group(identity, signal.SIGKILL)

    @abstractmethod
    def identity(self, pid: int) -> ProcessIdentity: ...

    def started_at(self, identity: ProcessIdentity) -> float:
        """This recorded process's birth on the monotonic clock."""
        raise NotImplementedError("This platform does not expose process elapsed time")

    @abstractmethod
    def arguments(self, process: ProcessIdentity) -> tuple[str, ...]: ...

    @abstractmethod
    def environment(self, process: ProcessIdentity) -> dict[str, str]: ...

    @abstractmethod
    def process_name(self, pid: int) -> str: ...

    def lock_holders(self, descriptor: int) -> tuple[LockHolder, ...]:
        return (UnidentifiedLockHolder(),)

    def parent_identity(self, pid: int) -> ProcessIdentity:
        raise NotImplementedError('This platform does not expose exact parent identities')

    async def wait_exited(self, identity: ProcessIdentity) -> None:
        """Wait for one incarnation's exit; members with a loop-readable handle override this."""
        await asyncio.to_thread(self.wait_any_exited, (identity,), math.inf)

    @abstractmethod
    def wait_any_exited(
        self, identities: tuple[ProcessIdentity, ...], timeout: float,
    ) -> None:
        """Wait for a member's exit or the caller's remaining deadline."""

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

    def send(self, identity: ProcessIdentity, signum: int) -> None:
        self.require(identity)
        os.kill(identity.pid, signum)

    def group_members(self, leader: ProcessIdentity) -> tuple[ProcessIdentity, ...]:
        try:
            current = self.identity(leader.pid)
        except ProcessLookupError:
            return leader.members(self)
        if current != leader:
            raise IdentityMismatchError(f"Process incarnation changed: {leader.pid}")
        return current.members(self)

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

    def parent_death_binding(self) -> int:
        """Darwin has no parent-death signal: its exec gate binds no launcher."""
        return 0

    @abstractmethod
    def members_of_group(self, pgid: int) -> tuple[ProcessIdentity, ...]: ...


@dataclass(frozen=True)
class LinuxProcessRecord:
    identity: ProcessIdentity
    group: int
    parent: int


class LinuxPlatform(PidfdHandles, Platform):
    def require_watched_procfs(self, refusal: Exception) -> None:
        return None

    @contextmanager
    def socket_address(self, path: Path, descriptor: int) -> Generator[Path, None, None]:
        yield Path(f"/proc/{os.getpid()}/fd/{descriptor}") / path.name

    def parent_death_binding(self) -> int:
        """The exec gate arms PR_SET_PDEATHSIG and binds to this launcher's PID."""
        return os.getpid()

    @staticmethod
    def _stat(pid: int) -> LinuxProcessRecord:
        try:
            # comm can contain whitespace and ')'; fields after its final ')'
            # start at field 3. Kernel starttime is field 22, pgrp is field 5.
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        except FileNotFoundError:
            raise ProcessLookupError(pid) from None
        if fields[0] in {"Z", "X"}:
            raise ProcessLookupError(pid)
        group = int(fields[2])
        return LinuxProcessRecord(
            ProcessIdentity.in_group(pid, int(fields[19]), group), group, int(fields[1]))

    def identity(self, pid: int) -> ProcessIdentity:
        return self._stat(pid).identity

    def started_at(self, identity: ProcessIdentity) -> float:
        started = identity.start_time / os.sysconf("SC_CLK_TCK")
        age = time.clock_gettime(time.CLOCK_BOOTTIME) - started
        return time.monotonic() - age

    def arguments(self, process: ProcessIdentity) -> tuple[str, ...]:
        try:
            raw = Path(f"/proc/{process.pid}/cmdline").read_bytes()
        except FileNotFoundError:
            raise ProcessLookupError(process.pid) from None
        if not raw:
            raise ProcessLookupError(process.pid)
        return tuple(os.fsdecode(word) for word in raw.split(b"\0")[:-1])

    def environment(self, process: ProcessIdentity) -> dict[str, str]:
        try:
            raw = Path(f"/proc/{process.pid}/environ").read_bytes()
        except FileNotFoundError:
            raise ProcessLookupError(process.pid) from None
        values = (item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
        return {os.fsdecode(key): os.fsdecode(value) for key, value in values}

    def process_name(self, pid: int) -> str:
        try:
            return Path(f"/proc/{pid}/comm").read_text().strip()
        except FileNotFoundError:
            raise ProcessLookupError(pid) from None

    def lock_holders(self, descriptor: int) -> tuple[LockHolder, ...]:
        """Identify actual flock owners of this opened file, not merely open FDs.

        An OFD lock with no reported PID, inaccessible /proc, or a concurrent exit
        is unknown; absence never proves sole ownership of the lock.
        """
        info = os.fstat(descriptor)
        identity = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
        holders = []
        with open('/proc/locks', encoding='ascii') as locks:
            for line in locks:
                fields = line.split()
                if len(fields) < 8 or fields[1] != 'FLOCK' or fields[5] != identity:
                    continue
                pid = int(fields[4])
                if pid < 0:
                    holders.append(UnidentifiedLockHolder())
                    continue
                try:
                    command = " ".join(self.arguments(self.identity(pid)))
                except ProcessLookupError:
                    holders.append(ExitedLockHolder(pid))
                else:
                    holders.append(RunningLockHolder(pid, command))
        return tuple(holders)

    def parent_identity(self, pid: int) -> ProcessIdentity:
        return self.identity(self._stat(pid).parent)

    async def wait_exited(self, identity: ProcessIdentity) -> None:
        try:
            descriptor = self.open_pidfd(identity.pid)
        except ProcessLookupError:
            return
        try:
            self.require(identity)
            loop = asyncio.get_running_loop()
            exited = loop.create_future()
            def observe() -> None:
                if not exited.done():
                    exited.set_result(None)
            loop.add_reader(descriptor, observe)
            try:
                await exited
            finally:
                loop.remove_reader(descriptor)
        except ProcessLookupError:
            return
        finally:
            os.close(descriptor)

    def wait_any_exited(
        self, identities: tuple[ProcessIdentity, ...], timeout: float,
    ) -> None:
        descriptors: list[int] = []
        try:
            pending = select.poll()
            for identity in identities:
                try:
                    descriptor = self.open_pidfd(identity.pid)
                    descriptors.append(descriptor)
                    current = self.identity(identity.pid)
                except ProcessLookupError:
                    return
                if current != identity:
                    return  # Another incarnation holds the pid: this member exited.
                pending.register(descriptor, select.POLLIN)
            pending.poll(None if math.isinf(timeout) else max(0, math.ceil(timeout * 1000)))
        finally:
            for descriptor in descriptors:
                os.close(descriptor)

    def members_of_group(self, pgid: int) -> tuple[ProcessIdentity, ...]:
        members = []
        for entry in Path("/proc").iterdir():
            if entry.name.isdecimal():
                with suppress(ProcessLookupError):
                    record = self._stat(int(entry.name))
                    if record.group == pgid:
                        members.append(record.identity)
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


class DarwinPlatform(Platform):
    """libproc exposes microsecond birth times, unlike formatted ps output."""

    def require_watched_procfs(self, refusal: Exception) -> None:
        raise refusal

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

    def started_at(self, identity: ProcessIdentity) -> float:
        return time.monotonic() - (time.time() - identity.start_time / 1_000_000)

    def arguments(self, process: ProcessIdentity) -> tuple[str, ...]:
        raise NotImplementedError(f"{type(self).__name__} does not read process arguments")

    def environment(self, process: ProcessIdentity) -> dict[str, str]:
        raise NotImplementedError(f"{type(self).__name__} does not read process environment")

    def process_name(self, pid: int) -> str:
        raise NotImplementedError(f"{type(self).__name__} does not read process names")

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

    def wait_any_exited(
        self, identities: tuple[ProcessIdentity, ...], timeout: float,
    ) -> None:
        with select.kqueue() as pending:
            events = [
                select.kevent(
                    identity.pid, filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                    fflags=select.KQ_NOTE_EXIT,
                )
                for identity in identities
            ]
            for identity in identities:
                try:
                    current = self.identity(identity.pid)
                except ProcessLookupError:
                    return
                if current != identity:
                    return  # Another incarnation holds the pid: this member exited.
            try:
                returned = pending.control(
                    events, 1, None if math.isinf(timeout) else max(0, timeout),
                )
                for event in returned:
                    if event.flags & select.KQ_EV_ERROR:
                        raise OSError(event.data, os.strerror(event.data))
            except ProcessLookupError:
                return


class ResidentManager(DeclaredFamily, affix="Manager"):
    """What owns a handed-off resident once its launcher is gone: a host fact, decoded once.

    Leaving the launcher's process tree is not enough when the launcher is itself a systemd
    unit: ending that unit signals its whole cgroup, setsid and the reparent to init included.
    """

    @staticmethod
    def current() -> type[ResidentManager]:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime and Path(runtime, "systemd", "private").is_socket():
            return SystemdUserManager
        return NoResidentManager

    @classmethod
    def command(cls, command: tuple[str, ...]) -> tuple[str, ...]:
        """A host with no manager owns the resident only through its process tree."""
        return command


class NoResidentManager(ResidentManager): ...


class SystemdUserManager(ResidentManager):
    """The user manager owns each resident in its own transient scope; ``systemd-run
    --scope`` execs in place, so the gate's handed-off PID is the resident's."""

    @classmethod
    def command(cls, command: tuple[str, ...]) -> tuple[str, ...]:
        return ("systemd-run", "--user", "--scope", "--quiet", "--collect",
                f"--unit=agent-comms-resident-{Id128.mint()}", "--", *command)


class ChildLifetime(DeclaredFamily, affix="Lifetime"):
    """Whether a launched child may outlive the process that launched it."""

    handoff: ClassVar[bool] = False

    @classmethod
    def command(cls, command: tuple[str, ...]) -> tuple[str, ...]:
        """The command the exec gate executes; a child bound to its launcher runs it as given."""
        return command

    @classmethod
    def spawned(cls, process: subprocess.Popen[bytes], descriptor: int,
                registration: ChildRegistration) -> SpawnedProcess:
        return SpawnedProcess(process, Platform.current().identity(process.pid), registration)

    @classmethod
    async def spawned_async(cls, process: asyncio.subprocess.Process, descriptor: int,
                            registration: ChildRegistration) -> AttachedChild:
        return AttachedChild(process, Platform.current().identity(process.pid), registration)

    @classmethod
    @abstractmethod
    def launcher(cls, platform: Platform) -> int:
        """The PID the exec gate binds the child's life to; zero binds none."""


class DiesWithLauncherLifetime(ChildLifetime):
    """Every child by default: a killed launcher cannot leave it running under init.

    The kernel signal follows the launching thread, so the thread that launches
    the child must outlive it, as the event loop and a pool worker's task do.
    """

    @classmethod
    def launcher(cls, platform: Platform) -> int:
        return platform.parent_death_binding()


class OutlivesLauncherLifetime(ChildLifetime):
    """A resident the launcher hands off, such as a worker, a watcher or a bridge."""

    handoff: ClassVar[bool] = True

    @classmethod
    def command(cls, command: tuple[str, ...]) -> tuple[str, ...]:
        return ResidentManager.current().command(command)

    @staticmethod
    def resident(descriptor: int) -> ProcessIdentity:
        encoded = os.read(descriptor, 64)
        if not encoded:
            raise ChildProcessError("Exec gate exited without handing off its resident")
        return Platform.current().identity(int(encoded))

    @classmethod
    def spawned(cls, process: subprocess.Popen[bytes], descriptor: int,
                registration: ChildRegistration) -> SpawnedProcess:
        try:
            identity = cls.resident(descriptor)
        finally:
            returncode = process.wait()
        if returncode != 0:
            raise ChildProcessError("Resident handoff failed")
        return HandedOffProcess(process, identity, registration)

    @classmethod
    async def spawned_async(cls, process: asyncio.subprocess.Process, descriptor: int,
                            registration: ChildRegistration) -> AttachedChild:
        try:
            identity = await asyncio.to_thread(cls.resident, descriptor)
        finally:
            returncode = await process.wait()
        if returncode != 0:
            raise ChildProcessError("Resident handoff failed")
        return HandedOffAttachedChild(process, identity, registration)

    @classmethod
    def launcher(cls, platform: Platform) -> int:
        return 0


class ChildRegistration(ABC):
    """The durable owner notified after a launched group has finished."""

    @abstractmethod
    def finished(self, identity: ProcessIdentity) -> None: ...


class NoChildRegistration(ChildRegistration):
    """An observed process has no launch registration to remove."""

    def finished(self, identity: ProcessIdentity) -> None:
        pass


NO_CHILD_REGISTRATION = NoChildRegistration()


class ChildLaunch:
    @property
    def children_store(self) -> type[RuntimeChildrenStore]:
        from .runtime_children import RuntimeChildrenStore

        return RuntimeChildrenStore

    def __init__(self, platform: Platform, command: tuple[str, ...],
                 pass_fds: tuple[int, ...], runtime: LaunchRuntime,
                 lifetime: type[ChildLifetime]):
        self.runtime = runtime
        self.platform = platform
        self.command = command
        self.pass_fds = pass_fds
        self.lifetime = lifetime
        self.read_fd, self.write_fd = os.pipe()
        self.error_r, error_w = os.pipe()
        self.error_writer = os.fdopen(error_w, "wb", buffering=0)
        self.identity_r, identity_w = os.pipe()
        self.identity_writer = os.fdopen(identity_w, "wb", buffering=0)

    def record(self, identity: ProcessIdentity, reservation: RuntimeChildReservation) -> None:
        """Persist the gated child, or cancel before user code executes."""
        try:
            reservation.record(identity)
        except BaseException:
            self.cancel_before_release(identity)
            raise

    @property
    def argv(self) -> tuple[str, ...]:
        # The exec gate runs this decoder's code before exec of the selected command.
        return (
            str(LaunchRuntime.running().interpreter),
            "-c",
            _EXEC_GATE,
            str(self.read_fd),
            str(self.error_writer.fileno()),
            str(self.lifetime.launcher(self.platform)),
            str(int(self.lifetime.handoff)),
            str(self.identity_writer.fileno()),
            *self.lifetime.command(self.command),
        )

    @property
    def options(self) -> dict:
        return {
            "start_new_session": True,
            "pass_fds": (self.read_fd, self.error_writer.fileno(),
                         self.identity_writer.fileno(), *self.pass_fds),
        }

    def spawned(self, process: subprocess.Popen[bytes], registration: ChildRegistration
                ) -> SpawnedProcess:
        self.identity_writer.close()
        return self.lifetime.spawned(process, self.identity_r, registration)

    async def spawned_async(self, process: asyncio.subprocess.Process,
                            registration: ChildRegistration) -> AttachedChild:
        self.identity_writer.close()
        return await self.lifetime.spawned_async(process, self.identity_r, registration)

    def spawn(
        self,
        *,
        cwd=None,
        env: Mapping[str, str] = INHERITED_ENVIRONMENT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) -> SpawnedProcess:
        """Record the launch handle before the platform allows exec."""
        with self.children_store.launching(self.runtime) as reservation:
            process = subprocess.Popen(
                self.argv, cwd=cwd, env=self.runtime.child_environment(env), stdin=stdin,
                stdout=stdout, stderr=stderr, **self.options,
            )
            child = self.spawned(process, reservation.store)
            try:
                self.record(child.identity, reservation)
            except BaseException:
                child.reap()
                child.close_streams()
                raise
        return child

    async def spawn_async(self, *, cwd, env, stdio: ChildStdio) -> AttachedChild:
        async with self.children_store.launching_async(self.runtime) as reservation:
            process = await asyncio.create_subprocess_exec(
                self.argv[0], *self.argv[1:], cwd=cwd, env=self.runtime.child_environment(env),
                **stdio.options, **self.options,
            )
            child = await self.spawned_async(process, reservation.store)
            child._stderr_task = asyncio.create_task(child._drain_stderr())
            try:
                self.record(child.identity, reservation)
            except BaseException:
                await process.wait()
                await child._stderr_task
                raise
        return child

    def release(self, identity: ProcessIdentity) -> None:
        self.error_writer.close()
        os.write(self.write_fd, b"G")

    def cancel_before_release(self, identity: ProcessIdentity) -> None:
        # The exec gate has not released user code or spawned descendants.
        with suppress(ProcessLookupError):
            self.platform.send(identity, signal.SIGKILL)

    def verify(self) -> None:
        error = os.read(self.error_r, 64)
        if error:
            code = int(error)
            raise OSError(code, os.strerror(code), self.command[0])

    async def verify_async(self) -> None:
        await _exec_error(self.error_r, self.command[0])

    def close(self) -> None:
        self.error_writer.close()
        self.identity_writer.close()
        for fd in (self.read_fd, self.write_fd, self.error_r, self.identity_r):
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class OrderCheckFailedError(ValueError):
    """A completed order check did not prove its expected result."""


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
            raise OrderCheckFailedError(f"The order check is {self.count} away from {expected}.")

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
        raise OrderCheckFailedError(
            f"The order check cannot prove {expected}: it printed no integer.")

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
    interrupted: ClassVar[bool] = False

    def report_shutdown(self, report_timeout: Callable[[], None]) -> None:
        pass

    def report_forced(self, report: Callable[[str], None]) -> None:
        pass

    def report_stopped(self, report: Callable[[], None], stop_exits: frozenset[int]) -> None:
        """Only a signal, or a program's declared stop exit, ends a child as stopped."""

    def fork_failure(self, interrupted_consequence: Callable[[], str]) -> str:
        return ""

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

    def report_stopped(self, report: Callable[[], None], stop_exits: frozenset[int]) -> None:
        if self.code in stop_exits:
            report()

    @property
    def summary(self) -> str:
        return f"exited with code {self.code}"

    @property
    def successful(self) -> bool:
        return self.code == 0


class InterruptedForkOutcome(ChildOutcome):
    """A killed helper may leave its native writer locks behind."""

    def fork_failure(self, interrupted_consequence: Callable[[], str]) -> str:
        return interrupted_consequence()


@dataclass(frozen=True)
class SignaledOutcome(InterruptedForkOutcome):
    interrupted: ClassVar[bool] = True
    signal_number: int

    def report_stopped(self, report: Callable[[], None], stop_exits: frozenset[int]) -> None:
        report()

    @property
    def summary(self) -> str:
        return f"stopped by {signal.Signals(self.signal_number).name}"

    @property
    def successful(self) -> bool:
        return False


@dataclass(frozen=True)
class TimedOutOutcome(InterruptedForkOutcome):
    termination: ChildOutcome

    def report_shutdown(self, report_timeout: Callable[[], None]) -> None:
        report_timeout()

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

    def report_forced(self, report: Callable[[str], None]) -> None:
        report(self.summary)


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
fd=int(sys.argv[1]); error_fd=int(sys.argv[2]); launcher=int(sys.argv[3])
handoff=int(sys.argv[4]); identity_fd=int(sys.argv[5])
if handoff:
    ready_r, ready_w = os.pipe()
    resident = os.fork()
    if resident:
        os.close(ready_w)
        ready = os.read(ready_r, 1); os.close(ready_r)
        if ready != b'R': os._exit(126)
        os.write(identity_fd, str(resident).encode()); os.close(identity_fd)
        os._exit(0)
    os.close(ready_r)
    os.setsid()
    os.write(ready_w, b'R'); os.close(ready_w)
os.close(identity_fd)
if launcher:
    import ctypes
    # PR_SET_PDEATHSIG (1) survives exec: the kernel kills this child with its launcher.
    if ctypes.CDLL(None, use_errno=True).prctl(1, 9):
        os.write(error_fd, str(ctypes.get_errno()).encode()); os._exit(126)
    if os.getppid() != launcher: os._exit(126)
go=os.read(fd,1); os.close(fd)
if go != b'G': sys.exit(126)
os.set_inheritable(error_fd, False)
try: os.execvpe(sys.argv[6], sys.argv[6:], os.environ)
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
    ) -> Generator[tuple[tuple[ProcessIdentity, ...], float], None, type[GracefulStopOutcome]]:
        with guard():
            self.platform.terminate_group(self.identity)
        grace_deadline = min(time.monotonic() + STOP_GRACE_SECONDS, deadline)
        stage = GracefulStopOutcome
        while members := self.platform.group_members(self.identity):
            now = time.monotonic()
            if stage is GracefulStopOutcome and now >= grace_deadline:
                stage = ForcedStopOutcome
                with guard():
                    self.platform.force_group(self.identity)
                continue
            if stage is ForcedStopOutcome:
                if now >= deadline:
                    raise ChildRetirementFailed(f"Process group still alive: {members}")
                with guard():
                    self.platform.force_group(self.identity)
            # Re-enumerate after a member exits: a surviving member may have forked.
            until = grace_deadline if stage is GracefulStopOutcome else deadline
            yield members, max(0, until - now)
        self.registration.finished(self.identity)
        return stage

    def _retire_group(self, guard, deadline=math.inf):
        """Use the same guarded stop plan for synchronous and asynchronous cleanup."""
        plan = self._stop_plan(guard, deadline)
        while True:
            try:
                members, timeout = next(plan)
            except StopIteration as done:
                return done.value
            self.platform.wait_any_exited(members, timeout)

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
        env: Mapping[str, str] = INHERITED_ENVIRONMENT,
        pass_fds: tuple[int, ...] = (),
        stdio: ChildStdio = StreamingChildStdio(),
        runtime: LaunchRuntime = LaunchRuntime.running(),
        lifetime: type[ChildLifetime] = DiesWithLauncherLifetime,
    ) -> AttachedChild:
        if not command:
            raise ValueError("A child command is required")
        platform = Platform.current()
        with platform.launch(command, pass_fds, runtime, lifetime) as launch:
            child = await launch.spawn_async(cwd=cwd, env=env, stdio=stdio)
            try:
                launch.release(child.identity)
                await launch.verify_async()
            except BaseException:
                await child.stop()
                raise
        return child

    def send(self, data: bytes) -> None:
        """Write synchronously while this child's input still exists and it runs."""
        if self.stdin is None or self.returncode is not None:
            raise BrokenPipeError
        self.stdin.write(data)

    async def drain(self) -> None:
        await self.stdin.drain()

    async def write(self, data: bytes) -> None:
        """Write to this still-running child's input and observe transport backpressure."""
        self.send(data)
        await self.drain()

    async def send_fenced(self, data: bytes, boundary, *, measurements) -> None:
        """Keep the independent raw writer on this child's original input."""
        from .native_prompt_send import send_fenced_prompt

        await send_fenced_prompt(self.stdin, data, boundary, measurements=measurements)

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


class HandedOffAttachedChild(AttachedChild):
    """The gate was reaped; resident completion has no parent exit status."""

    @property
    def returncode(self) -> None:
        return None

    async def wait(self) -> ChildOutcome:
        await self.identity.wait_exited()
        await asyncio.shield(self._stderr_task)
        return DetachedExitOutcome()


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

    @staticmethod
    def watch(descriptor: int, lost: Callable[[], None]) -> None:
        os.fstat(descriptor)

        def parent_watchdog() -> None:
            try:
                os.read(descriptor, 1)
            finally:
                lost()

        threading.Thread(target=parent_watchdog, daemon=True).start()

    @classmethod
    def guard(cls) -> None:
        cls.watch(int(os.environ.pop(cls.variable)), partial(os._exit, 1))


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
        env: Mapping[str, str] = INHERITED_ENVIRONMENT,
        pass_fds: tuple[int, ...] = (),
        runtime: LaunchRuntime = LaunchRuntime.running(),
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
        return SynchronousProcess.stop_together(((self, guard),), deadline=deadline)[0]

    @staticmethod
    def stop_together(stops, *, deadline=math.inf) -> tuple[ChildOutcome, ...]:
        """Step every member's stop plan together: one signal pass, one shared grace, one force pass.

        Each member still runs the sole stop plan under its own guard. Expected
        identity and retirement failures are raised once every other plan has
        finished; storage, ownership and programming faults propagate immediately.
        """
        errors: list[Exception] = []
        plans = {}
        for index, (process, guard) in enumerate(stops):
            try:
                process.require_stop_authority()
            except ProcessLookupError as error:
                errors.append(error)
                continue
            plans[index] = process._stop_plan(guard, deadline)
        stages = {}
        while plans:
            waits = []
            for index, plan in tuple(plans.items()):
                try:
                    waits.append(next(plan))
                except StopIteration as done:
                    stages[index] = done.value
                    del plans[index]
                except ChildRetirementFailed as error:
                    errors.append(error)
                    del plans[index]
            if waits:
                members = tuple(dict.fromkeys(
                    member for identities, _timeout in waits for member in identities
                ))
                Platform.current().wait_any_exited(
                    members, min(timeout for _identities, timeout in waits),
                )
        if errors:
            raise errors[0] if len(errors) == 1 else ExceptionGroup("Process retirement failed", errors)
        return tuple(stages[index](process.reap()) for index, (process, _) in enumerate(stops))


class ObservedProcess(SynchronousProcess):
    """A recorded incarnation: may signal it, cannot claim a parent's exit code."""

    def require_stop_authority(self) -> None:
        self.identity = self.platform.require(self.identity)

    def reap(self) -> ChildOutcome:
        return DetachedExitOutcome()

    async def wait(self) -> ChildOutcome:
        await self.platform.wait_exited(self.identity)
        return self.reap()


class RecordedRuntimeGroup(ObservedProcess):
    """A recorded launch owns the group even after its original leader exits."""

    def require_stop_authority(self) -> None:
        self.platform.group_members(self.identity)


class SpawnedProcess(SynchronousProcess):
    """A launched process handle; surviving the launcher does not preserve its parent."""

    def __init__(self, process: subprocess.Popen[bytes], identity: ProcessIdentity,
                 registration: ChildRegistration = NO_CHILD_REGISTRATION):
        super().__init__(identity, registration)
        self.process = process

    @classmethod
    @contextmanager
    def launching(
        cls,
        command: tuple[str, ...],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] = INHERITED_ENVIRONMENT,
        stdin: int | BinaryIO | TextIO = subprocess.DEVNULL,
        output: int | BinaryIO | TextIO = subprocess.DEVNULL,
        stderr: int | BinaryIO | TextIO = subprocess.STDOUT,
        runtime: LaunchRuntime = LaunchRuntime.running(),
        lifetime: type[ChildLifetime] = DiesWithLauncherLifetime,
    ) -> Generator[SpawnedProcess]:
        """Yield the child before it executes; leaving the body releases it, raising reaps it."""
        if not command:
            raise ValueError("A child command is required")
        with Platform.current().launch(command, (), runtime, lifetime) as launch:
            child = launch.spawn(cwd=cwd, env=env, stdin=stdin, stdout=output,
                                 stderr=stderr)
            try:
                yield child
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

    @classmethod
    def launch(cls, command: tuple[str, ...], **options: Any) -> SpawnedProcess:
        """Launch with nothing to reserve before the child executes."""
        with cls.launching(command, **options) as child:
            return child

    def collect(self, *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        """Collect within the caller's owned context; its exit retires descendants."""
        stdout, stderr = self.process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(self.process.args, self.process.wait(), stdout, stderr)

    @classmethod
    def run(
        cls, command: tuple[str, ...], *, timeout: float,
        cwd: str | Path, env: dict[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        """A one-shot interpreter exchange, including forced descendant retirement.

        Containers use launch/collect instead: their graceful scope exit lets
        nested owners retire their own groups before the outer force pass.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("A positive finite timeout is required")
        deadline = time.monotonic() + timeout
        with cls.launch(command, cwd=cwd, env=env, output=subprocess.PIPE,
                        stderr=subprocess.PIPE) as child:
            try:
                return child.collect(timeout=max(0, deadline - time.monotonic()))
            finally:
                child.platform.force_group(child.identity)

    def reap(self) -> ChildOutcome:
        return ChildOutcome.from_returncode(self.process.wait(timeout=STOP_GRACE_SECONDS))

    async def wait(self) -> ChildOutcome:
        await self.platform.wait_exited(self.identity)
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

    def __enter__(self) -> SpawnedProcess:
        return self

    def __exit__(self, *_):
        self.close()


class HandedOffProcess(ObservedProcess, SpawnedProcess):
    """Keep launch streams, while observed-process behavior owns resident retirement."""

    def collect(self, *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        raise ChildProcessError("A handed-off resident has no parent exit status to collect")


class PoolWorkerStopped(BaseException):
    """A pool's parent stopped; unwind owned work before the worker exits."""


class ForkedProcessPool(ExitStack, ProcessPoolExecutor, ParentLifeline):
    """Parallel work whose workers and nested child owners cannot outlive its parent."""

    def __init__(self, workers: int) -> None:
        """The caller declares the width; no default multiplies it under a parallel runner."""
        if workers < 1:
            raise ValueError("A pool needs at least one worker")
        ExitStack.__init__(self)
        ParentLifeline.__enter__(self)
        self.callback(ParentLifeline.__exit__, self)
        try:
            ProcessPoolExecutor.__init__(
                self, max_workers=workers, mp_context=multiprocessing.get_context("fork"),
                initializer=self._initialize, initargs=(self.read_fd, self.write_fd),
            )
        except BaseException:
            self.close()
            raise
        self.callback(ProcessPoolExecutor.shutdown, self)

    @staticmethod
    def _stopped(signum: int, frame: object) -> NoReturn:
        # A second notification must not interrupt the current child's retirement.
        signal.signal(signum, signal.SIG_IGN)
        raise PoolWorkerStopped()

    @classmethod
    def _initialize(cls, read_fd: int, write_fd: int) -> None:
        signal.signal(signal.SIGTERM, cls._stopped)
        os.close(write_fd)
        cls.watch(read_fd, partial(os.kill, os.getpid(), signal.SIGTERM))

    @staticmethod
    def _work[T](work: Callable[[], T]) -> T:
        try:
            return work()
        except PoolWorkerStopped:
            # ProcessPoolExecutor otherwise catches BaseException and waits for
            # another task from the dead parent. Owned scopes have now unwound.
            os._exit(1)

    def submit[T, **P](
        self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs,
    ) -> Future[T]:
        return super().submit(self._work, partial(fn, *args, **kwargs))


@dataclass(frozen=True)
class DetachedExitOutcome(ChildOutcome):
    """Exit observed; a non-parent cannot claim an exit code."""

    @property
    def summary(self) -> str:
        return "exited; status unavailable"

    @property
    def successful(self) -> bool:
        return False


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
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.execvpe(self.command[0], self.command, os.environ)


