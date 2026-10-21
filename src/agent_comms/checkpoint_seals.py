"""Typed private-prefix seals; decoded shape alone is never index authority."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .declared_family import DeclaredFamily
from .errors import CheckpointNeedsRepairError, RelationViolationError
from .field_codec import Id128Text, Sha256Text
from .private_path import FileIdentity, FileRevision

if TYPE_CHECKING:
    import os
    import sqlite3

    from . import private_bus_checkpoint
    from .wire_log import WireLog
    from .wire_metadata import WireMetadata

@dataclass(frozen=True)
class PrefixSource:
    """The original logical root and opened physical file, compared as one value."""

    root_id: str
    device: int
    inode: int


@dataclass(frozen=True)
class PrefixSeal:
    root_id: str
    revision: FileRevision
    through_seq: int
    digest: str
    tail: str

    @property
    def source_identity(self) -> PrefixSource:
        return PrefixSource(
            self.root_id, self.revision.identity.device, self.revision.identity.inode
        )

    def __post_init__(self) -> None:
        Id128Text.from_text(self.root_id)
        if self.through_seq < 0:
            raise ValueError("Invalid prefix revision/sequence")
        Sha256Text.from_text(self.digest)
        Sha256Text.from_text(self.tail)


@dataclass(frozen=True)
class CheckpointSeal(DeclaredFamily, affix="Seal"):
    family_discriminator = "state"
    version: Literal[1]
    db_revision: FileRevision

    @abstractmethod
    def recover(
        self,
        bus: WireLog,
        marker: WireMetadata,
        db: sqlite3.Connection,
        path: Path,
        saved: private_bus_checkpoint.PrefixWitness,
        info: os.stat_result,
    ) -> private_bus_checkpoint.PrefixWitness:
        return saved.index_tail(bus, marker, db, path, info)

    @abstractmethod
    def recovery_end(self, info: os.stat_result) -> int: ...

    @abstractmethod
    def chunk_complete(self, offset: int, size: int, rows: int) -> bool: ...

    def check_final(self, saved: private_bus_checkpoint.PrefixWitness, db_path: Path) -> None:
        raise RelationViolationError("Private bus checkpoint index seal changed or is pending.")

    @abstractmethod
    def require_rebuilt(self, saved: private_bus_checkpoint.PrefixWitness,
                        rebuilt: private_bus_checkpoint.PrefixWitness) -> None:
        """Check the rebuilt index matches what this write said it would be."""

    @abstractmethod
    def require_final(self, saved: private_bus_checkpoint.PrefixWitness, db_path: Path) -> None:
        """Readers holding the shared lock accept only a final seal; repair needs the exclusive lock."""

    @abstractmethod
    def rebuild_source(self) -> PrefixSeal:
        """The canonical byte cut that must be proved before rebuilding derived rows."""


@dataclass(frozen=True)
class FinalSeal(CheckpointSeal):
    witness: PrefixSeal

    @classmethod
    def capture(cls, witness: private_bus_checkpoint.PrefixWitness, db_path: Path) -> FinalSeal:
        return cls(1, FileRevision.from_stat(db_path.stat()), witness.seal())

    def check_final(self, saved: private_bus_checkpoint.PrefixWitness, db_path: Path) -> None:
        if self.witness != saved.seal():
            raise RelationViolationError("Private bus checkpoint index seal changed or is pending.")
        current = FileRevision.from_stat(db_path.stat())
        if self.db_revision != current:
            if (current.size, current.mtime_ns) == (self.db_revision.size, self.db_revision.mtime_ns):
                raise CheckpointNeedsRepairError("Private bus checkpoint index relocated.")
            raise RelationViolationError("Private bus checkpoint index seal changed or is pending.")

    def recover(
        self,
        bus: WireLog,
        marker: WireMetadata,
        db: sqlite3.Connection,
        path: Path,
        saved: private_bus_checkpoint.PrefixWitness,
        info: os.stat_result,
    ) -> private_bus_checkpoint.PrefixWitness:
        if saved.revision.identity != FileIdentity.from_stat(info):
            self.require_rebuilt(saved, saved)
            copied = FileRevision.from_stat(path.stat())
            if (copied.size, copied.mtime_ns) != (self.db_revision.size, self.db_revision.mtime_ns):
                return saved.rebuild_from_empty(bus, marker, db, path, info)
            saved = saved.rebind_source(bus, marker, db, info)
        else:
            try:
                self.check_final(saved, path)
            except CheckpointNeedsRepairError:
                saved.require_prefix(bus, info)
                self.require_rebuilt(saved, saved)
                marker.seal_with(FinalSeal.capture(saved, path))
                bus.write_metadata_unlocked(marker)
        if saved.revision == FileRevision.from_stat(info):
            return saved
        return super().recover(bus, marker, db, path, saved, info)

    def recovery_end(self, info: os.stat_result) -> int:
        return info.st_size

    def chunk_complete(self, offset: int, size: int, rows: int) -> bool:
        return size >= 16 * 1024 * 1024 or rows >= 10_000

    def require_rebuilt(self, saved: private_bus_checkpoint.PrefixWitness,
                        rebuilt: private_bus_checkpoint.PrefixWitness) -> None:
        if self.witness != saved.seal():
            raise RelationViolationError("Private bus checkpoint prefix record differs from its seal.")

    def require_final(self, saved: private_bus_checkpoint.PrefixWitness, db_path: Path) -> None:
        self.check_final(saved, db_path)

    def rebuild_source(self) -> PrefixSeal:
        return self.witness


@dataclass(frozen=True)
class PendingSeal(CheckpointSeal):
    prior: PrefixSeal
    expected: PrefixSeal

    @classmethod
    def capture(cls, prior: private_bus_checkpoint.PrefixWitness,
                expected: private_bus_checkpoint.PrefixWitness, db_path: Path) -> PendingSeal:
        return cls(1, FileRevision.from_stat(db_path.stat()), prior.seal(), expected.seal())

    def recover(
        self,
        bus: WireLog,
        marker: WireMetadata,
        db: sqlite3.Connection,
        path: Path,
        saved: private_bus_checkpoint.PrefixWitness,
        info: os.stat_result,
    ) -> private_bus_checkpoint.PrefixWitness:
        relocated = saved.revision.identity != FileIdentity.from_stat(info)
        contains_cut = (
            info.st_size >= self.expected.revision.size if relocated
            else self.expected.revision.holds_cut(
                FileRevision.from_stat(info), self.expected.revision.size)
        )
        if (saved.seal() not in (self.prior, self.expected)
                or not contains_cut
                or self.prior.root_id != marker.root_id):
            raise RelationViolationError("Private bus checkpoint pending intent is inconsistent.")
        if relocated:
            # Preserve the pending intent until its entire suffix is proved and
            # committed; a zero-suffix rebind cannot complete a pending append.
            saved.require_prefix(bus, info)
        return super().recover(bus, marker, db, path, saved, info)

    def recovery_end(self, info: os.stat_result) -> int:
        return self.expected.revision.size

    def chunk_complete(self, offset: int, size: int, rows: int) -> bool:
        return offset == self.expected.revision.size

    def require_rebuilt(self, saved: private_bus_checkpoint.PrefixWitness,
                        rebuilt: private_bus_checkpoint.PrefixWitness) -> None:
        if (
            saved.seal() not in (self.prior, self.expected)
            or rebuilt.offset != self.expected.revision.size
            or replace(rebuilt.seal(), revision=self.expected.revision) != self.expected
        ):
            raise RelationViolationError("Private bus checkpoint pending suffix differs from intent.")

    def require_final(self, saved: private_bus_checkpoint.PrefixWitness, db_path: Path) -> None:
        raise CheckpointNeedsRepairError("Private bus checkpoint has an unfinished write.")

    def rebuild_source(self) -> PrefixSeal:
        # An interrupted append may complete only after its entire intended cut is proved.
        return self.expected
