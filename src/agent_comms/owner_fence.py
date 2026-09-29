"""Attempt authority bound to an owner generation and revision."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS,
    require_bounded,
    require_nonempty,
    validate_execution_id,
)


@dataclass(frozen=True, slots=True)
class OwnerFence:
    execution_id: str
    attempt_ordinal: int
    owner_thread: str
    owner_generation: int
    revision: int
    token: str

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        for field, value in (("owner_thread", self.owner_thread), ("token", self.token)):
            require_nonempty(value, field)
            require_bounded(value, field, MAX_IDENTIFIER_CHARS)
        if min(self.attempt_ordinal, self.owner_generation, self.revision) <= 0:
            raise ValueError("fence ordinal, generation and revision must be positive")


def prepare_fence_token() -> str:
    """Deliver the CSPRNG secret to its owner *before* any database transaction."""
    return secrets.token_hex(32)


def _digest(token: str) -> str:
    if not isinstance(token, str) or not 1 <= len(token) <= MAX_IDENTIFIER_CHARS:
        raise ValueError("invalid retained fence token")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
