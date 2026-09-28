"""Canonical bus marker declaration; WireLog owns its read/publication boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .audience_manifest import MAX_WIRE_SEQ
from .checkpoint_seals import CheckpointSeal
from .errors import RelationViolationError


@dataclass
class WireMetadata:
    last_seq: int = 0
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
        if self.private != (self.wire_root_id is not None):
            raise ValueError("Private bus protocol needs its root identity")
        if self.wire_root_id is not None and (
            len(self.wire_root_id) != 32
            or any(c not in "0123456789abcdef" for c in self.wire_root_id)
        ):
            raise ValueError("Invalid private bus root identity")
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
