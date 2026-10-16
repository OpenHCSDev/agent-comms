"""Canonical bus marker declaration; WireLog owns its read/publication boundary."""

from __future__ import annotations

import re
from abc import abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from .audience_manifest import MAX_WIRE_SEQ
from .checkpoint_seals import CheckpointSeal
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import TextRepresentation

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path
    from typing import BinaryIO

    from .bus_page_index import BusPageIndex
    from .private_bus_checkpoint import PrefixWitness
    from .wire_log import OpenedWireSnapshot, WireLog


class WireRootIdText(TextRepresentation):
    """The private bus declaration owns its external root identity spelling."""

    @classmethod
    def encode(cls, value: object) -> object:
        return cls.decode(value)

    @classmethod
    def from_text(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{32}", value) is None:
            raise ValueError("Invalid private bus root identity")
        return value


class WireAccess(DeclaredFamily, affix="Access"):
    @abstractmethod
    def require_append(self) -> None: ...

    @abstractmethod
    def open_checkpoint(self, path: Path, *, shared: bool) -> sqlite3.Connection: ...

    @abstractmethod
    def verify_checkpoint(self, bus: WireLog, marker: WireMetadata, db: sqlite3.Connection,
                          stream: BinaryIO, path: Path, *, shared: bool) -> PrefixWitness: ...

    @abstractmethod
    def open_page_index(self, path: Path) -> BusPageIndex: ...

    @contextmanager
    def prepare_page_index(self, index: BusPageIndex, source: OpenedWireSnapshot) -> Iterator[None]:
        self._prepare_index(index, source)
        with index.snapshot():
            source.require_page_index_coverage(index)
            yield

    @abstractmethod
    def _prepare_index(self, index: BusPageIndex, source: OpenedWireSnapshot) -> None: ...

    @abstractmethod
    def repair_page_index(self, index: BusPageIndex) -> None: ...


@dataclass(frozen=True)
class WritableAccess(WireAccess):
    def require_append(self) -> None:
        pass

    def open_checkpoint(self, path, *, shared):
        from .private_bus_checkpoint import _connect

        return _connect(path, readonly=shared)

    def verify_checkpoint(self, bus, marker, db, stream, path, *, shared):
        from .private_bus_checkpoint import (
            _verify_open_checkpoint_unlocked,
            _check_final_checkpoint_read_only,
        )

        if shared:
            return _check_final_checkpoint_read_only(bus, marker, db, stream, path)
        return _verify_open_checkpoint_unlocked(bus, marker, db, stream, path)

    def open_page_index(self, path):
        from .bus_page_index import BusPageIndex

        return BusPageIndex(path)

    def _prepare_index(self, index, source):
        index.prepare(source)

    def repair_page_index(self, index):
        index.invalidate()


@dataclass(frozen=True)
class ArchivedAccess(WireAccess):
    def require_append(self) -> None:
        raise RelationViolationError("Archived history is read-only.")

    def open_checkpoint(self, path, *, shared):
        from .private_bus_checkpoint import _connect

        return _connect(path, readonly=True)

    def verify_checkpoint(self, bus, marker, db, stream, path, *, shared):
        from .private_bus_checkpoint import CertifiedSourceRead, PrefixCertificate, _tail

        # The immutable certificate binds the original bytes and sidecar, not
        # the current writer's disposable index membership. No recovery writes
        # or suffix adoption are permitted in an archived source.
        saved = PrefixCertificate.read_witness(db)
        CertifiedSourceRead(bus.path, marker, db, stream, saved).require_current()
        if _tail(stream, saved.offset) != saved.tail:
            raise RelationViolationError("Archived source prefix tail changed.")
        return saved

    def open_page_index(self, path):
        from .bus_page_index import BusPageIndex

        return BusPageIndex(path, readonly=True)

    def _prepare_index(self, index, source):
        # Published archive indexes are immutable; the parent checks coverage.
        return None

    def repair_page_index(self, index):
        raise RelationViolationError("Published archive page index requires offline preparation")


@dataclass
class WireMetadata:
    last_seq: int = 0
    admission_after_seq: int = field(default=0, metadata={"wire_required": True})
    access: WireAccess = field(default_factory=WritableAccess, metadata={"wire_required": True})
    writer_protocol_version: Literal[1] | None = field(
        default=None, metadata={"wire_omit_default": True}
    )
    wire_root_id: str | None = field(default=None, metadata={"wire_omit_default": True})
    claim_envelopes_version: Literal[1] | None = field(
        default=None, metadata={"wire_omit_default": True}
    )
    checkpoint_version: Literal[1] | None = field(
        default=None, metadata={"wire_omit_default": True}
    )
    checkpoint_seal: CheckpointSeal | None = field(
        default=None, metadata={"wire_omit_default": True}
    )

    def __post_init__(self) -> None:
        if not 0 <= self.last_seq <= MAX_WIRE_SEQ:
            raise ValueError("Bus sequence is outside the durable range")
        if not 0 <= self.admission_after_seq <= self.last_seq:
            raise ValueError("Admission floor is outside the durable source range")
        if self.private != (self.wire_root_id is not None):
            raise ValueError("Private bus protocol needs its root identity")
        if self.wire_root_id is not None:
            WireRootIdText.from_text(self.wire_root_id)
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
