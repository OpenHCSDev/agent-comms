"""POSIX observations and private path roles, never enrollment authority.

These values describe the observed filesystem. Possessing one cannot mint a
fresh native session, accept an input or replace a returned durable acknowledgement.
"""

from __future__ import annotations

import os
import stat
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, ClassVar

from .child_process import Platform
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import NullMember, RecordMember, SubclassUnion


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
        return ObservedPath.observe(path.stat)

    @staticmethod
    def observe(read_stat: Callable[[], os.stat_result]) -> ObservedPath:
        """Decode a pathname or pinned-descriptor observation at the boundary."""
        try:
            observed = read_stat()
        except FileNotFoundError:
            return AbsentPath()
        return PresentPath(FileIdentity.from_stat(observed))

    @abstractmethod
    def same_file(self, identity: FileIdentity) -> bool: ...

    def is_absent(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class AbsentPath(ObservedPath):
    def same_file(self, identity: FileIdentity) -> bool:
        return False

    def is_absent(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class PresentPath(ObservedPath):
    identity: FileIdentity

    def same_file(self, identity: FileIdentity) -> bool:
        return self.identity == identity


class FileObservation(SubclassUnion, affix=""):
    """A path's stat revision when observed, or its absence."""

    @abstractmethod
    def contains_cut(self, earlier: FileRevision, cut: int) -> bool:
        """Whether this observation still holds ``earlier``'s append-only byte cut."""

    @abstractmethod
    def lost_in(self, current: FileObservation) -> bool:
        """A missing source, replacement or truncation does not preserve this cut."""

    @abstractmethod
    def revisions(self) -> tuple[FileRevision, ...]:
        """The observed revision, or none for a missing file."""

    @abstractmethod
    def require(self, refusal: Exception) -> FileRevision:
        """The observed revision; a missing file raises ``refusal``."""

    @abstractmethod
    def either[Answer](
        self, present: Callable[[FileRevision], Answer], missing: Callable[[], Answer],
    ) -> Answer:
        """Answer from the observed revision, or for the missing file."""

    @abstractmethod
    def file_identity(self) -> FileIdentity | MissingFile:
        """The file this observation saw, or the absence it saw."""

    @abstractmethod
    def holds_inode(self, inode: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class MissingFile(NullMember, FileObservation):
    def contains_cut(self, earlier: FileRevision, cut: int) -> bool:
        return False

    def lost_in(self, current: FileObservation) -> bool:
        return current != self

    def revisions(self) -> tuple[FileRevision, ...]:
        return ()

    def require(self, refusal: Exception) -> FileRevision:
        raise refusal

    def either[Answer](
        self, present: Callable[[FileRevision], Answer], missing: Callable[[], Answer],
    ) -> Answer:
        return missing()

    def file_identity(self) -> MissingFile:
        return self

    def holds_inode(self, inode: int) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class FileRevision(RecordMember, FileObservation):
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
    def capture(cls, path: Path) -> FileObservation:
        try:
            return cls.from_stat(path.stat())
        except FileNotFoundError:
            return MissingFile()

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

    def contains_cut(self, earlier: FileRevision, cut: int) -> bool:
        return earlier.holds_cut(self, cut)

    def lost_in(self, current: FileObservation) -> bool:
        return not current.contains_cut(self, self.size)

    def revisions(self) -> tuple[FileRevision, ...]:
        return (self,)

    def require(self, refusal: Exception) -> FileRevision:
        return self

    def either[Answer](
        self, present: Callable[[FileRevision], Answer], missing: Callable[[], Answer],
    ) -> Answer:
        return present(self)

    def file_identity(self) -> FileIdentity:
        return self.identity

    def holds_inode(self, inode: int) -> bool:
        return self.identity.inode == inode

    def opened_by(self, stream: BinaryIO) -> bool:
        """Whether an opened file still contains this captured byte cut."""
        return self.holds_cut(type(self).from_file(stream), self.size)


class FilesystemRole(DeclaredFamily, affix="Role"):
    kind_flags: ClassVar[int]

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
    def accepts_links(cls, nlink: int) -> bool:
        return True

    @classmethod
    def violation(cls, observed: os.stat_result) -> type[FilesystemRule] | None:
        return next((rule for rule in FilesystemRule.members_with(FilesystemRule)
                     if rule.violated(cls, observed)), None)

    @classmethod
    def require(cls, observed: os.stat_result, refusal: Exception) -> None:
        failed = cls.violation(observed)
        if failed is not None:
            raise refusal from RelationViolationError(f"{cls.declared_name}: {failed.declared_name}")

    @classmethod
    def _opened(
        cls, path: str | Path, flags: int, refusal: Exception, *,
        dir_fd: int | None = None, mode: int = 0o600,
    ) -> int:
        Platform.current().require_pinned_paths(refusal)
        fd = os.open(path, flags | os.O_NOFOLLOW | cls.kind_flags, mode, dir_fd=dir_fd)
        try:
            cls.require(os.fstat(fd), refusal)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @classmethod
    def read(cls, path: str | Path, refusal: Exception, *, dir_fd: int | None = None) -> int:
        return cls._opened(path, os.O_RDONLY, refusal, dir_fd=dir_fd)

    @classmethod
    def update(cls, path: str | Path, refusal: Exception, *, dir_fd: int | None = None) -> int:
        return cls._opened(path, os.O_RDWR, refusal, dir_fd=dir_fd)

    @classmethod
    def held(cls, fd: int, path: Path, refusal: Exception) -> None:
        observed = os.fstat(fd)
        cls.require(observed, refusal)
        if FileIdentity.from_stat(observed) != FileIdentity.from_stat(path.lstat()):
            raise refusal


class PrivateRole(FilesystemRole):
    permissions: ClassVar[int]

    @classmethod
    def accepts_permissions(cls, mode: int) -> bool:
        return stat.S_IMODE(mode) == cls.permissions

    @classmethod
    def create(cls, path: str | Path, refusal: Exception, *, dir_fd: int | None = None) -> int:
        return cls._opened(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, refusal,
                           dir_fd=dir_fd, mode=cls.permissions)


class PrivateFileRole(PrivateRole):
    permissions = 0o600

    @classmethod
    def accepts_kind(cls, mode: int) -> bool:
        return stat.S_ISREG(mode)


    @classmethod
    def accepts_links(cls, nlink: int) -> bool:
        return nlink == 1


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
        refusal = ValueError("Socket directory changed during acquisition")
        fd = TrustedAncestorRole.read(path.parent, refusal)
        try:
            TrustedAncestorRole.held(fd, path.parent, refusal)
            with Platform.current().socket_address(path, fd) as address:
                yield address
        finally:
            os.close(fd)


class OwnedDirectoryRole(FilesystemRole):
    @classmethod
    def accepts_kind(cls, mode: int) -> bool:
        return stat.S_ISDIR(mode)

    @classmethod
    def accepts_permissions(cls, mode: int) -> bool:
        return True


class TrustedAncestorRole(FilesystemRole):
    @classmethod
    def require_path(cls, directory: Path, refusal: Exception) -> None:
        for ancestor in (directory, *directory.parents):
            cls.require(ancestor.lstat(), refusal)

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


class MultipleLinksRule(FilesystemRule):
    @classmethod
    def violated(cls, role, observed):
        return not role.accepts_links(observed.st_nlink)
