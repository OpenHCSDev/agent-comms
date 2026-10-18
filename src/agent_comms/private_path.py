"""POSIX observations and private path roles, never enrollment authority.

These values describe the observed filesystem. Possessing one cannot mint a
fresh native session, accept an input or replace a returned durable acknowledgement.
"""

from __future__ import annotations

import os
import stat
import sys
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO, ClassVar
from collections.abc import Iterator

from .declared_family import DeclaredFamily
from .errors import RelationViolationError


def fsync_directory(path: Path) -> None:
    """Make a directory's entries durable without following a final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int

    def __post_init__(self) -> None:
        if self.device < 0 or self.inode < 0:
            raise ValueError("File identity fields must be nonnegative")

    @classmethod
    def from_stat(cls, observed: os.stat_result) -> FileIdentity:
        return cls(observed.st_dev, observed.st_ino)


class ObservedPath(ABC):
    @staticmethod
    def at(path: Path) -> ObservedPath:
        try:
            observed = path.stat()
        except FileNotFoundError:
            return AbsentPath()
        return PresentPath(FileIdentity.from_stat(observed))

    @abstractmethod
    def same_file(self, identity: FileIdentity) -> bool: ...


@dataclass(frozen=True, slots=True)
class AbsentPath(ObservedPath):
    def same_file(self, identity: FileIdentity) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class PresentPath(ObservedPath):
    identity: FileIdentity

    def same_file(self, identity: FileIdentity) -> bool:
        return self.identity == identity


@dataclass(frozen=True, slots=True)
class FileRevision:
    identity: FileIdentity
    size: int
    mtime_ns: int
    ctime_ns: int

    def __post_init__(self) -> None:
        if self.size < 0 or self.mtime_ns < 0 or self.ctime_ns < 0:
            raise ValueError("File revision fields must be nonnegative")

    @classmethod
    def from_stat(cls, observed: os.stat_result) -> FileRevision:
        return cls(
            FileIdentity.from_stat(observed), observed.st_size,
            observed.st_mtime_ns, observed.st_ctime_ns,
        )

    @classmethod
    def from_file(cls, stream: BinaryIO) -> FileRevision:
        return cls.from_stat(os.fstat(stream.fileno()))

    @classmethod
    def capture(cls, path: Path) -> FileRevision | None:
        try:
            return cls.from_stat(path.stat())
        except FileNotFoundError:
            return None

    def holds_cut(self, current: FileRevision, cut: int) -> bool:
        """Whether current still contains this append-only captured byte cut."""
        return (
            self.identity == current.identity
            and current.size >= cut
            and (
                current.size > self.size
                or (current.mtime_ns, current.ctime_ns) == (self.mtime_ns, self.ctime_ns)
            )
        )

    @staticmethod
    def source_changed(earlier: FileRevision | None, current: FileRevision | None) -> bool:
        """A missing source, replacement or truncation does not preserve its cut."""
        if earlier is None or current is None:
            return earlier != current
        return not earlier.holds_cut(current, earlier.size)

    def opened_by(self, stream: BinaryIO) -> bool:
        """Whether an opened file still contains this captured byte cut."""
        return self.holds_cut(type(self).from_file(stream), self.size)


class FilesystemRole(DeclaredFamily, affix="Role"):
    @classmethod
    @abstractmethod
    def accepts_kind(cls, mode: int) -> bool: ...

    @classmethod
    def accepts_owner(cls, uid: int) -> bool:
        return uid == os.geteuid()

    @classmethod
    @abstractmethod
    def accepts_permissions(cls, mode: int) -> bool: ...

    @classmethod
    def violation(cls, observed: os.stat_result) -> type[FilesystemRule] | None:
        return next((rule for rule in FilesystemRule.members_with(FilesystemRule)
                     if rule.violated(cls, observed)), None)

    @classmethod
    def require(cls, observed: os.stat_result) -> None:
        failed = cls.violation(observed)
        if failed is not None:
            raise RelationViolationError(f"{cls.declared_name}: {failed.declared_name}")


class PrivateRole(FilesystemRole):
    permissions: ClassVar[int]

    @classmethod
    def accepts_permissions(cls, mode: int) -> bool:
        return stat.S_IMODE(mode) == cls.permissions


class PrivateFileRole(PrivateRole):
    permissions = 0o600

    @classmethod
    def accepts_kind(cls, mode: int) -> bool:
        return stat.S_ISREG(mode)


class PrivateDirectoryRole(PrivateRole):
    permissions = 0o700

    @classmethod
    def accepts_kind(cls, mode: int) -> bool:
        return stat.S_ISDIR(mode)


class PrivateSocketRole(PrivateRole):
    permissions = 0o600

    @classmethod
    def accepts_kind(cls, mode: int) -> bool:
        return stat.S_ISSOCK(mode)

    @classmethod
    @contextmanager
    def address(cls, path: Path) -> Iterator[Path]:
        """Borrow a directory, not its arbitrarily long pathname, for AF_UNIX.

        The socket inode remains at path. Linux resolves this short proc address
        through the held directory FD; native children may use it while the
        listener owns that FD. Reply paths never derive from this address.
        POSIX platforms without proc use a private, temporary directory link
        for the same inode. No socket or acknowledgement moves into that directory.
        """
        path = Path(path).absolute()
        observed = path.parent.lstat()
        TrustedAncestorRole.require(observed)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if not os.path.samestat(observed, os.fstat(fd)):
                raise ValueError("Socket directory changed during acquisition")
            if sys.platform.startswith("linux"):
                yield Path(f"/proc/{os.getpid()}/fd/{fd}") / path.name
            else:
                with TemporaryDirectory(prefix="ac-socket-", dir="/tmp") as directory:
                    parent = Path(directory) / "d"
                    parent.symlink_to(path.parent, target_is_directory=True)
                    if not os.path.samestat(os.stat(parent), os.fstat(fd)):
                        raise ValueError("Socket directory changed during address acquisition")
                    yield parent / path.name
        finally:
            os.close(fd)


class TrustedAncestorRole(FilesystemRole):
    @classmethod
    def accepts_kind(cls, mode: int) -> bool:
        return stat.S_ISDIR(mode)

    @classmethod
    def accepts_owner(cls, uid: int) -> bool:
        return uid in (0, os.geteuid())

    @classmethod
    def accepts_permissions(cls, mode: int) -> bool:
        return not mode & 0o022 or bool(mode & stat.S_ISVTX)


class FilesystemRule(DeclaredFamily, affix="Rule"):
    @classmethod
    @abstractmethod
    def violated(cls, role: type[FilesystemRole], observed: os.stat_result) -> bool: ...


class WrongNodeKindRule(FilesystemRule):
    @classmethod
    def violated(cls, role, observed):
        return not role.accepts_kind(observed.st_mode)


class ForeignNodeOwnerRule(FilesystemRule):
    @classmethod
    def violated(cls, role, observed):
        return not role.accepts_owner(observed.st_uid)


class UnsafeNodePermissionsRule(FilesystemRule):
    @classmethod
    def violated(cls, role, observed):
        return not role.accepts_permissions(observed.st_mode)
