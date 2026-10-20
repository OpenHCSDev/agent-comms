"""Canonical bus marker declaration; WireLog owns its read/publication boundary."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar, Literal, Annotated


from .audience_manifest import MAX_WIRE_SEQ
from .checkpoint_seals import CheckpointSeal
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import (
    Id128Text,
    NullMember,
    SingleFieldValue,
    WireName,
    WireOmitDefault,
    WireRequired,
    WireValue,
)
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


class MarkerFact(WireValue):
    """One optional bus_meta.json fact; each member answers whether the marker declares it."""

    __slots__ = ()
    declared: ClassVar[bool] = True


@dataclass(frozen=True, slots=True)
class UndeclaredMarker(NullMember, MarkerFact):
    """bus_meta.json leaves this protocol out; it is written back out the same way."""

    declared: ClassVar[bool] = False

    def root(self) -> str:
        raise RelationViolationError("Private bus writer has no durable protocol marker.")

    def seal(self) -> CheckpointSeal:
        raise RelationViolationError("Private checkpoint lacks durable marker binding.")


@dataclass(frozen=True)
class DeclaredVersion(SingleFieldValue, MarkerFact):
    """The marker declares a protocol at its one version."""

    version: Literal[1] = 1


@dataclass(frozen=True)
class DeclaredRoot(SingleFieldValue, MarkerFact):
    root_id: str

    def root(self) -> str:
        return self.root_id


@dataclass(frozen=True)
class DeclaredSeal(SingleFieldValue, MarkerFact):
    checkpoint_seal: CheckpointSeal

    def seal(self) -> CheckpointSeal:
        return self.checkpoint_seal


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
        DeclaredVersion | UndeclaredMarker, WireOmitDefault()
    ] = UndeclaredMarker()
    wire_root_id: Annotated[DeclaredRoot | UndeclaredMarker, WireOmitDefault()] = (
        UndeclaredMarker()
    )
    claim_envelopes_version: Annotated[
        DeclaredVersion | UndeclaredMarker, WireOmitDefault()
    ] = UndeclaredMarker()
    checkpoint_version: Annotated[DeclaredVersion | UndeclaredMarker, WireOmitDefault()] = (
        UndeclaredMarker()
    )
    checkpoint_seal: Annotated[DeclaredSeal | UndeclaredMarker, WireOmitDefault()] = (
        UndeclaredMarker()
    )

    def __post_init__(self) -> None:
        if not 0 <= self.last_seq <= MAX_WIRE_SEQ:
            raise ValueError("Bus sequence is outside the durable range")
        if not 0 <= self.claimed_after_seq <= self.last_seq:
            raise ValueError("Claim floor is outside the durable source range")
        if self.private != self.wire_root_id.declared:
            raise ValueError("Private bus protocol needs its root identity")
        if self.private:
            Id128Text.from_text(self.root_id)
        if self.claims and not self.private:
            raise ValueError("Claim marker requires private protocol")
        if self.checkpoint_version.declared != self.requires_checkpoint:
            raise ValueError("Checkpoint requires its durable marker binding")
        if self.requires_checkpoint and not self.claims:
            raise ValueError("Private checkpoint lacks its claim read barrier")

    @classmethod
    def private_root(cls, root_id: str, *, claims: bool = False, **facts) -> WireMetadata:
        """A private bus marker for one root, optionally with the claim read barrier."""
        return cls(
            writer_protocol_version=DeclaredVersion(),
            wire_root_id=DeclaredRoot(root_id),
            claim_envelopes_version=DeclaredVersion() if claims else UndeclaredMarker(),
            **facts,
        )

    @property
    def private(self) -> bool:
        return self.writer_protocol_version.declared

    @property
    def claims(self) -> bool:
        return self.claim_envelopes_version.declared

    @property
    def requires_checkpoint(self) -> bool:
        """The original marker declares whether its durable seal is required."""
        return self.checkpoint_seal.declared

    @property
    def root_id(self) -> str:
        return self.wire_root_id.root()

    @property
    def seal(self) -> CheckpointSeal:
        return self.checkpoint_seal.seal()

    def seal_with(self, seal: CheckpointSeal) -> None:
        if not self.claims:
            raise RelationViolationError("Checkpoint requires the claim read barrier.")
        self.checkpoint_version = DeclaredVersion()
        self.checkpoint_seal = DeclaredSeal(seal)

    def unseal(self) -> None:
        """Drop the checkpoint binding so a fresh checkpoint can be installed."""
        self.checkpoint_version = UndeclaredMarker()
        self.checkpoint_seal = UndeclaredMarker()

    def unsealed(self) -> WireMetadata:
        """This marker's facts apart from its checkpoint binding."""
        return replace(
            self, checkpoint_version=UndeclaredMarker(), checkpoint_seal=UndeclaredMarker()
        )
