"""Typed private-prefix seals; decoded shape alone is never index authority."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .declared_family import DeclaredFamily
from .errors import RelationViolationError

if TYPE_CHECKING:
    import os
    import sqlite3

    from .private_bus_checkpoint import PrefixWitness
    from .wire_log import WireLog
    from .wire_metadata import WireMetadata

Revision = tuple[int, int, int, int, int]


def file_revision(info: os.stat_result) -> Revision:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass(frozen=True)
class PrefixSource:
    """The original logical root and opened physical file, compared as one value."""

    root_id: str
    device: int
    inode: int


@dataclass(frozen=True)
class PrefixSeal:
    root_id: str
    revision: Revision
    through_seq: int
    digest: str
    tail: str

    @property
    def source_identity(self) -> PrefixSource:
        return PrefixSource(self.root_id, self.revision[0], self.revision[1])

    def __post_init__(self) -> None:
        if len(self.root_id) != 32 or any(c not in "0123456789abcdef" for c in self.root_id):
            raise ValueError("Invalid prefix root identity")
        if any(item < 0 for item in self.revision) or self.through_seq < 0:
            raise ValueError("Invalid prefix revision/sequence")
        if any(
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for value in (self.digest, self.tail)
        ):
            raise ValueError("Invalid prefix digest")


@dataclass(frozen=True)
class CheckpointSeal(DeclaredFamily, affix="Seal"):
    family_discriminator = "state"
    version: Literal[1]
    db_revision: Revision

    def __post_init__(self) -> None:
        if any(item < 0 for item in self.db_revision):
            raise ValueError("Invalid checkpoint index revision")

    @abstractmethod
    def recover(
        self,
        bus: WireLog,
        marker: WireMetadata,
        db: sqlite3.Connection,
        path: Path,
        saved: PrefixWitness,
        info: os.stat_result,
    ) -> PrefixWitness | None: ...

    def check_final(self, saved: PrefixWitness, db_path: Path) -> None:
        raise RelationViolationError("Private bus checkpoint index seal changed or is pending.")

    @abstractmethod
    def require_final(self, saved: PrefixWitness, db_path: Path) -> None:
        """Readers holding the shared lock accept only a final seal; repair needs the exclusive lock."""


@dataclass(frozen=True)
class FinalSeal(CheckpointSeal):
    witness: PrefixSeal

    @classmethod
    def capture(cls, witness: PrefixWitness, db_path: Path) -> FinalSeal:
        return cls(1, file_revision(db_path.stat()), witness.seal())

    def check_final(self, saved: PrefixWitness, db_path: Path) -> None:
        if self.witness != saved.seal() or self.db_revision != file_revision(db_path.stat()):
            raise RelationViolationError("Private bus checkpoint index seal changed or is pending.")

    def recover(
        self,
        bus: WireLog,
        marker: WireMetadata,
        db: sqlite3.Connection,
        path: Path,
        saved: PrefixWitness,
        info: os.stat_result,
    ) -> None:
        self.check_final(saved, path)

    def require_final(self, saved: PrefixWitness, db_path: Path) -> None:
        self.check_final(saved, db_path)


@dataclass(frozen=True)
class PendingSeal(CheckpointSeal):
    prior: PrefixSeal
    expected: PrefixSeal

    @classmethod
    def capture(cls, prior: PrefixWitness, expected: PrefixWitness, db_path: Path) -> PendingSeal:
        return cls(1, file_revision(db_path.stat()), prior.seal(), expected.seal())

    def recover(
        self,
        bus: WireLog,
        marker: WireMetadata,
        db: sqlite3.Connection,
        path: Path,
        saved: PrefixWitness,
        info: os.stat_result,
    ) -> PrefixWitness:
        from .private_bus_checkpoint import _recover_pending_unlocked

        if (
            saved.seal() not in (self.prior, self.expected)
            or self.expected.revision != file_revision(info)
            or self.prior.root_id != marker.root_id
        ):
            raise RelationViolationError("Private bus checkpoint pending intent is inconsistent.")
        return _recover_pending_unlocked(bus, marker, db, path, info, self)

    def require_final(self, saved: PrefixWitness, db_path: Path) -> None:
        from .private_bus_checkpoint import CheckpointNeedsRepair

        raise CheckpointNeedsRepair("Private bus checkpoint has an unfinished write.")
