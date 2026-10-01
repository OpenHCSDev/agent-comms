"""POSIX observations and private path roles, never enrollment authority.

These values describe the observed filesystem. Possessing one cannot mint a
fresh native session, admit an input or replace a returned durable receipt.
"""

from __future__ import annotations

import os
import stat
from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

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
