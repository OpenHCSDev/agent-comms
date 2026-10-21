"""Response identity and envelope validation, without Message import dependencies."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS, require_bounded, require_nonempty, validate_execution_id,
)
from agent_comms.field_codec import MalformedRecord

_PRIVATE_WIRE_PREFIX: Final = "_agent_comms_private"


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
    if has_private_wire_fields(record):
        raise MalformedRecord("Digest input must be a public envelope.")
    return hashlib.sha256(_canonical(record)).hexdigest()


def has_private_wire_fields(record: Mapping[str, object]) -> bool:
    """Identify reserved private keys without inspecting their values."""
    return any(isinstance(key, str) and key.startswith(_PRIVATE_WIRE_PREFIX) for key in record)


def canonical_publication_key(execution_id: str, exact_target: str) -> str:
    """Return the sole deterministic publication identity for an obligation route."""
    validate_execution_id(execution_id)
    require_nonempty(exact_target, "exact_target")
    require_bounded(exact_target, "exact_target", MAX_IDENTIFIER_CHARS)
    key = f"publication:v1:{execution_id}:{exact_target}"
    require_bounded(key, "publication_key", MAX_IDENTIFIER_CHARS)
    return key


@dataclass(frozen=True)
class KeyedResponseAcknowledgement:
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

    def add_unique(self, seen_keys: set[str]) -> None:
        if self.publication_key in seen_keys:
            raise MalformedRecord("Duplicate response publication key.")
        seen_keys.add(self.publication_key)
