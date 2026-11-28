"""Host OS filesystem, durability, privacy and socket operations without process management."""

from __future__ import annotations

import ctypes
import os
import shutil
import socket
import stat
import sys
from abc import ABC, abstractmethod
from collections.abc import Generator, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

from agent_comms.nominal.declared_family import DeclaredFamily


class RelayEnding(ABC):
    """How a relay between a local connection and a WebSocket ended; the observation it
    carried reports it."""

    @abstractmethod
    def describe(self) -> str: ...


class LockHolder(RelayEnding, DeclaredFamily, affix="LockHolder"):
    """An observed flock owner; diagnosis, not authority to signal it."""

    @staticmethod
    def described(holders: tuple[LockHolder, ...]) -> str:
        return "held by " + "; ".join(holder.describe() for holder in holders)


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


class HostFilesystem:
    """POSIX host filesystem, durability, privacy and client-socket capabilities."""

    store_lock_interval = 0.025
    store_lock_first_wait = 0.0002

    @classmethod
    def store_lock_waits(cls) -> Iterator[float]:
        """How long each refused attempt at a store lock waits before the next: short at first,
        doubling up to the interval. A commit holds a store lock for about a millisecond, so a
        reader that slept the whole interval after one refusal waited 25 ms for a lock that was
        free in one: 0.59 s of a channel message's wall time in the worker alone."""
        wait = cls.store_lock_first_wait
        while True:
            yield wait
            wait = min(wait * 2, cls.store_lock_interval)

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

    class NativeImage(ctypes.Structure):
        """The POSIX loader's native symbol-image ABI."""

        _fields_ = (
            ("filename", ctypes.c_char_p),
            ("base", ctypes.c_void_p),
            ("symbol", ctypes.c_char_p),
            ("address", ctypes.c_void_p),
        )

    @contextmanager
    def native_image(self, address: ctypes.c_void_p) -> Generator[BufferedReader]:
        """Open the native file providing a symbol; the caller consumes this handle."""
        lookup = ctypes.CDLL(None).dladdr
        lookup.argtypes = (ctypes.c_void_p, ctypes.POINTER(self.NativeImage))
        lookup.restype = ctypes.c_int
        image = self.NativeImage()
        if not lookup(address, ctypes.byref(image)) or not image.filename:
            raise ValueError("The loader did not identify the native symbol image")
        with Path(os.fsdecode(image.filename)).resolve(strict=True).open("rb") as stream:
            yield stream

    def is_private(self, info: os.stat_result, permissions: int) -> bool:
        return stat.S_IMODE(info.st_mode) == permissions and self.is_owned(info)

    def is_owned(self, info: os.stat_result) -> bool:
        return info.st_uid == os.geteuid()

    def trusted_ancestor_owner(self, uid: int) -> bool:
        return uid in (0, os.geteuid())

    def ancestor_write_bits(self, info: os.stat_result) -> int:
        """Directory write permissions granted outside the owner's trusted principals."""
        return info.st_mode & 0o022

    def publish_new_file(
        self, staged: str | Path, destination: str | Path, *,
        src_dir_fd: int | None = None, dst_dir_fd: int | None = None,
    ) -> None:
        """Publish a closed stage without replacing a winner; the stage may be consumed."""
        os.link(staged, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
                follow_symlinks=False)

    def snapshot_file(
        self, source: str | Path, destination: str | Path, *,
        src_dir_fd: int | None = None, dst_dir_fd: int | None = None,
    ) -> None:
        """Retain an immutable file's bytes and mode under a new, exclusive name."""
        os.link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
                follow_symlinks=False)

    def share_file(self, source: Path, destination: Path) -> bool:
        """Share an immutable artifact's inode, or report that no sharing occurred."""
        os.link(source, destination, follow_symlinks=False)
        return True

    def make_private(self, path: Path, permissions: int) -> None:
        with suppress(FileNotFoundError):
            if stat.S_IMODE(path.lstat().st_mode) != permissions:
                os.chmod(path, permissions, follow_symlinks=False)

    def make_private_descriptor(self, descriptor: int, permissions: int) -> None:
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != permissions:
            os.fchmod(descriptor, permissions)

    @contextmanager
    def socket_address(self, path: Path, descriptor: int) -> Generator[Path]:
        with TemporaryDirectory(prefix="ac-socket-", dir="/tmp") as directory:
            parent = Path(directory) / "d"
            parent.symlink_to(path.parent, target_is_directory=True)
            if not os.path.samestat(os.stat(parent), os.fstat(descriptor)):
                raise ValueError("Socket directory changed during address acquisition")
            yield parent / path.name

    def configure_client_socket(self, sock: socket.socket) -> None:
        """Keep a client TCP socket responsive (no Nagle + delayed-ACK stall) and detect half-open switches."""
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def lock_holders(self, descriptor: int) -> tuple[LockHolder, ...]:
        return (UnidentifiedLockHolder(),)


class LinuxHostFilesystem(HostFilesystem):
    """Linux-specific TCP keepalive, procfs fd socket addresses and symbol mapping lookup."""

    CLIENT_KEEPALIVE_IDLE_SECONDS: ClassVar[int] = 10
    CLIENT_KEEPALIVE_INTERVAL_SECONDS: ClassVar[int] = 3
    CLIENT_KEEPALIVE_COUNT: ClassVar[int] = 2
    CLIENT_TCP_USER_TIMEOUT_MS: ClassVar[int] = 5_000

    def configure_client_socket(self, sock: socket.socket) -> None:
        super().configure_client_socket(sock)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, self.CLIENT_KEEPALIVE_IDLE_SECONDS)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, self.CLIENT_KEEPALIVE_INTERVAL_SECONDS)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, self.CLIENT_KEEPALIVE_COUNT)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, self.CLIENT_TCP_USER_TIMEOUT_MS)

    @contextmanager
    def native_image(self, address: ctypes.c_void_p) -> Generator[BufferedReader]:
        # glibc's main-image filename aliases argv[0]; setproctitle changes it.
        # Resolve the actual symbol mapping instead, including shared libpython.
        # ctypes represents C null as None; kernel mapping addresses are integers.
        pointer = address.value or 0
        if pointer == 0:
            raise ValueError("A null address has no native image")
        with Path("/proc/self/maps").open() as mappings:
            for line in mappings:
                extent, _permissions, _offset, device, inode, *_name = line.split(maxsplit=5)
                first, stop = (int(value, 16) for value in extent.split("-"))
                if first <= pointer < stop:
                    link = Path("/proc/self/map_files") / f"{first:x}-{stop:x}"
                    with link.resolve(strict=True).open("rb") as stream:
                        observed = os.fstat(stream.fileno())
                        major, minor = (int(value, 16) for value in device.split(":"))
                        # Decode kernel metadata once at this native boundary.
                        if (observed.st_dev, observed.st_ino) != (os.makedev(major, minor), int(inode)):
                            raise ValueError("The native image file no longer matches its mapping")
                        yield stream
                    return
        raise ValueError("The kernel did not identify the native symbol image")

    @contextmanager
    def socket_address(self, path: Path, descriptor: int) -> Generator[Path]:
        yield Path(f"/proc/{os.getpid()}/fd/{descriptor}") / path.name

    @staticmethod
    def _proc_cmdline(pid: int) -> tuple[str, ...]:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except FileNotFoundError:
            raise ProcessLookupError(pid) from None
        if not raw:
            raise ProcessLookupError(pid)
        return tuple(os.fsdecode(word) for word in raw.split(b"\0")[:-1])

    def _lock_holder_command(self, pid: int) -> tuple[str, ...]:
        return self._proc_cmdline(pid)

    def lock_holders(self, descriptor: int) -> tuple[LockHolder, ...]:
        """Identify actual flock owners of this opened file, not merely open FDs.

        An OFD lock with no reported PID, inaccessible /proc, or a concurrent exit
        is unknown; absence never proves sole ownership of the lock.
        """
        info = os.fstat(descriptor)
        identity = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
        holders = []
        with open("/proc/locks", encoding="ascii") as locks:
            for line in locks:
                fields = line.split()
                if len(fields) < 8 or fields[1] != "FLOCK" or fields[5] != identity:
                    continue
                pid = int(fields[4])
                if pid < 0:
                    holders.append(UnidentifiedLockHolder())
                    continue
                try:
                    command = " ".join(self._lock_holder_command(pid))
                except ProcessLookupError:
                    holders.append(ExitedLockHolder(pid))
                else:
                    holders.append(RunningLockHolder(pid, command))
        return tuple(holders)


class AndroidHostFilesystem(LinuxHostFilesystem):
    """Android filesystem semantics without kernel hard links."""

    # AOSP's AID_SYSTEM owns /data; app UID/GID pairs own their isolated sandboxes.
    system_identity = 1000

    def trusted_ancestor_owner(self, uid: int) -> bool:
        return uid == self.system_identity or super().trusted_ancestor_owner(uid)

    def ancestor_write_bits(self, info: os.stat_result) -> int:
        writes = super().ancestor_write_bits(info)
        if info.st_gid in (0, self.system_identity, os.getegid()):
            writes &= ~0o020
        return writes

    def publish_new_file(
        self, staged: str | Path, destination: str | Path, *,
        src_dir_fd: int | None = None, dst_dir_fd: int | None = None,
    ) -> None:
        # Android forbids hard links. renameat2 supplies the same no-replace decision
        # without opening the published SQLite inode and releasing another fd's locks.
        function = ctypes.CDLL(None, use_errno=True).renameat2
        function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                             ctypes.c_char_p, ctypes.c_uint)
        function.restype = ctypes.c_int
        if function(-100 if src_dir_fd is None else src_dir_fd, os.fsencode(staged),
                    -100 if dst_dir_fd is None else dst_dir_fd, os.fsencode(destination), 1) < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), os.fspath(destination))

    def snapshot_file(
        self, source: str | Path, destination: str | Path, *,
        src_dir_fd: int | None = None, dst_dir_fd: int | None = None,
    ) -> None:
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_dir_fd), "rb") as original:
            observed = os.fstat(original.fileno())
            if not stat.S_ISREG(observed.st_mode):
                raise ValueError("A retained snapshot requires a regular source file")
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=dst_dir_fd)
            try:
                with os.fdopen(descriptor, "wb") as retained:
                    shutil.copyfileobj(original, retained)
                    self.make_private_descriptor(retained.fileno(), stat.S_IMODE(observed.st_mode))
                    retained.flush()
                    os.fsync(retained.fileno())
            except BaseException:
                os.unlink(destination, dir_fd=dst_dir_fd)
                raise

    def share_file(self, source: Path, destination: Path) -> bool:
        return False  # A byte copy would not deliver the inode sharing this operation promises.


class HostPlatform(HostFilesystem, DeclaredFamily, affix="HostPlatform"):
    """Lightweight OS declaration for callers needing only filesystem or socket operations."""

    @classmethod
    def current(cls) -> HostPlatform:
        return cls.member_named(sys.platform)()


class LinuxHostPlatform(LinuxHostFilesystem, HostPlatform):
    """Linux host filesystem and socket platform."""


class AndroidHostPlatform(AndroidHostFilesystem, LinuxHostPlatform):
    """Android host filesystem and socket platform."""


class DarwinHostPlatform(HostPlatform):
    """Darwin host filesystem and socket platform."""
