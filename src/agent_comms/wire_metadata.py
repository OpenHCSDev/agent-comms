"""Canonical bus marker declaration; WireLog owns its read/publication boundary."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Literal, Annotated


from .audience_manifest import MAX_WIRE_SEQ
from .checkpoint_seals import CheckpointSeal
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import Id128Text, WireName, WireOmitDefault, WireRequired
from .locked_store import DurableFile

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path
    from typing import BinaryIO

    from .private_bus_checkpoint import PrefixWitness
    from .store_files import LockMode
    from .wire_log import WireLog


class WireAccess(DeclaredFamily, affix="Access"):
    @abstractmethod
    def require_append(self) -> None: ...

    @abstractmethod
    def open_checkpoint(self, path: Path, *, mode: LockMode) -> sqlite3.Connection: ...

    @abstractmethod
    def verify_checkpoint(self, bus: WireLog, marker: WireMetadata, db: sqlite3.Connection,
                          stream: BinaryIO, path: Path, *, mode: LockMode) -> PrefixWitness: ...


@dataclass(frozen=True)
class WritableAccess(WireAccess):
    def require_append(self) -> None:
        pass

    def open_checkpoint(self, path, *, mode):
        from .private_bus_checkpoint import WritableCheckpoint

        return mode.open_checkpoint(WritableCheckpoint(path))

    def verify_checkpoint(self, bus, marker, db, stream, path, *, mode):
        from .private_bus_checkpoint import WritableCheckpoint

        return mode.verify_checkpoint(WritableCheckpoint(path), bus, marker, db, stream)


@dataclass(frozen=True)
class ArchivedAccess(WireAccess):
    def require_append(self) -> None:
        raise RelationViolationError("Archived history is read-only.")

    def open_checkpoint(self, path, *, mode):
        from .private_bus_checkpoint import _connect

        return _connect(path, readonly=True)

    def verify_checkpoint(self, bus, marker, db, stream, path, *, mode):
        from .private_bus_checkpoint import CheckedPrefix, CheckedRead, _tail

        # The saved check binds the bus bytes and index file, not
        # the current writer's disposable index membership. No recovery writes
        # or suffix adoption are permitted in an archived source.
        saved = CheckedPrefix.read_witness(db)
        CheckedRead(bus.path, marker, db, stream, saved).require_current()
        if _tail(stream, saved.offset) != saved.tail:
            raise RelationViolationError("Archived source prefix tail changed.")
        return saved


@dataclass
class WireMetadata(DurableFile):
    filename: ClassVar[str] = "bus_meta.json"
    last_seq: int = 0
    claimed_after_seq: Annotated[
        int, WireName("admission_after_seq"), WireRequired()
    ] = 0
    access: Annotated[WireAccess, WireRequired()] = field(
        default_factory=WritableAccess
    )
    writer_protocol_version: Annotated[
        Literal[1] | None, WireOmitDefault()
    ] = None
    wire_root_id: Annotated[str | None, WireOmitDefault()] = None
    claim_envelopes_version: Annotated[
        Literal[1] | None, WireOmitDefault()
    ] = None
    checkpoint_version: Annotated[Literal[1] | None, WireOmitDefault()] = (
        None
    )
    checkpoint_seal: Annotated[CheckpointSeal | None, WireOmitDefault()] = (
        None
    )

    def __post_init__(self) -> None:
        if not 0 <= self.last_seq <= MAX_WIRE_SEQ:
            raise ValueError("Bus sequence is outside the durable range")
        if not 0 <= self.claimed_after_seq <= self.last_seq:
            raise ValueError("Claim floor is outside the durable source range")
        if self.private != (self.wire_root_id is not None):
            raise ValueError("Private bus protocol needs its root identity")
        if self.wire_root_id is not None:
            Id128Text.from_text(self.wire_root_id)
        if self.claims and not self.private:
            raise ValueError("Claim marker requires private protocol")
        if (self.checkpoint_version is not None) != (self.checkpoint_seal is not None):
            raise ValueError("Checkpoint requires its durable marker binding")
        if self.checkpoint_seal is not None and not self.claims:
            raise ValueError("Private checkpoint lacks its claim read barrier")

    @property
    def private(self) -> bool:
        return self.writer_protocol_version is not None

    @property
    def claims(self) -> bool:
        return self.claim_envelopes_version is not None

    @property
    def requires_checkpoint(self) -> bool:
        """The original marker declares whether its durable seal is required."""
        return self.checkpoint_seal is not None

    @property
    def root_id(self) -> str:
        if self.wire_root_id is None:
            raise RelationViolationError("Private bus writer has no durable protocol marker.")
        return self.wire_root_id

    @property
    def seal(self) -> CheckpointSeal:
        if self.checkpoint_seal is None:
            raise RelationViolationError("Private checkpoint lacks durable marker binding.")
        return self.checkpoint_seal

    def seal_with(self, seal: CheckpointSeal) -> None:
        if not self.claims:
            raise RelationViolationError("Checkpoint requires the claim read barrier.")
        self.checkpoint_version = 1
        self.checkpoint_seal = seal
