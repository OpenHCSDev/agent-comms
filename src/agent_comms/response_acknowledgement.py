"""Response identity and envelope validation, without Message import dependencies."""

from __future__ import annotations

import hashlib
import json
import math
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS, require_bounded, require_nonempty, validate_execution_id,
)
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import MalformedRecord, NullMember, RecordMember, SubclassUnion

def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def public_envelope_digest(record: Mapping[str, object]) -> str:
    """Bind every public wire field without including the private sideband."""
    timestamp = record.get("ts")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
    ):
        raise MalformedRecord("A keyed wire timestamp must be finite.")
    return hashlib.sha256(_canonical(record)).hexdigest()


def canonical_publication_key(execution_id: str, exact_target: str) -> str:
    """Return the sole deterministic publication identity for an obligation route."""
    validate_execution_id(execution_id)
    require_nonempty(exact_target, "exact_target")
    require_bounded(exact_target, "exact_target", MAX_IDENTIFIER_CHARS)
    key = f"publication:v1:{execution_id}:{exact_target}"
    require_bounded(key, "publication_key", MAX_IDENTIFIER_CHARS)
    return key


class ResponseAcknowledgement(SubclassUnion, affix="ResponseAcknowledgement"):
    """A keyed response's acknowledgement, or null on an original delivery."""

    @abstractmethod
    def require(self, root_id, message) -> Self:
        """This acknowledgement, proved against its wire root and public envelope."""

    @abstractmethod
    def answers(self, publication_key: str, execution_id: str) -> bool:
        """Whether this acknowledges that publication; one key under another execution conflicts."""


@dataclass(frozen=True)
class NoResponseAcknowledgement(NullMember, ResponseAcknowledgement):
    def require(self, root_id, message) -> Self:
        return self

    def answers(self, publication_key: str, execution_id: str) -> bool:
        return False


@dataclass(frozen=True)
class KeyedResponseAcknowledgement(RecordMember, ResponseAcknowledgement):
    wire_root_id: str
    execution_id: str
    publication_key: str
    envelope_digest: str

    def require(self, root_id, message):
        if self.wire_root_id != root_id:
            raise ValueError("Response acknowledgement belongs to another wire root.")
        if self.envelope_digest != public_envelope_digest(message.to_wire()):
            raise ValueError("Response acknowledgement public envelope digest differs.")
        if self.publication_key != canonical_publication_key(self.execution_id, message.target):
            raise ValueError("Response acknowledgement publication key differs from its execution/route.")
        return self

    def answers(self, publication_key: str, execution_id: str) -> bool:
        if self.publication_key != publication_key:
            return False
        if self.execution_id != execution_id:
            raise RelationViolationError("Response publication identity conflicts.")
        return True
