"""POSIX observations and private path roles, never enrollment authority.

These values describe the observed filesystem. Possessing one cannot mint a
fresh native session, admit an input or replace a returned durable receipt.
"""

from __future__ import annotations

import os
import stat
import sys
from abc import abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar
from collections.abc import Iterator

from .declared_family import DeclaredFamily


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int

    @classmethod
    def from_stat(cls, observed: os.stat_result) -> FileIdentity:
        return cls(observed.st_dev, observed.st_ino)


@dataclass(frozen=True, slots=True)
class FileRevision:
    identity: FileIdentity
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, observed: os.stat_result) -> FileRevision:
        return cls(
            FileIdentity.from_stat(observed), observed.st_size,
            observed.st_mtime_ns, observed.st_ctime_ns,
        )


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
            raise ValueError(f"{cls.declared_name}: {failed.declared_name}")


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
        listener owns that FD. Receipt paths never derive from this address.
        POSIX platforms without proc use a private, temporary directory link
        for the same inode. No socket or receipt moves into that directory.
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
