from __future__ import annotations

import os
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from agent_comms.nominal.field_codec import NullMember, RecordMember, SubclassUnion


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
    def capture(cls, path: Path, *, follow_symlinks: bool = True) -> FileObservation:
        try:
            return cls.from_stat(path.stat(follow_symlinks=follow_symlinks))
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


@dataclass(frozen=True, slots=True)
class DocumentBytes:
    """A file's revision and bytes from the same descriptor, before decoding."""

    revision: FileObservation
    content: bytes

    @classmethod
    def capture(cls, path: Path) -> DocumentBytes:
        try:
            with path.open("rb") as source:
                return cls(FileRevision.from_file(source), source.read())
        except FileNotFoundError:
            return cls(MissingFile(), b"")
