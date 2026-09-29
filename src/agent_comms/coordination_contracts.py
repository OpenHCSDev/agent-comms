"""Coordinator identity bounds and boundary validation."""

from __future__ import annotations

from typing import Final

RESOLVER_VERSION: Final = "resolver-v1"


POLICY_VERSION: Final = "policy-v1"


MAX_PUBLICATION_PAYLOAD_BYTES: Final = 120_000


MAX_REASON_CODE_CHARS: Final = 64


MAX_SANITIZED_DETAIL_CHARS: Final = 512


MAX_IDENTIFIER_CHARS: Final = 256


def require_nonempty(value: str, field: str) -> None:
    if not value:
        raise ValueError(f"{field} cannot be empty")


def require_bounded(value: str | None, field: str, maximum: int) -> None:
    if value is not None and len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")


def require_optional_nonempty(value: str | None, field: str, maximum: int) -> None:
    if value is not None:
        require_nonempty(value, field)
        require_bounded(value, field, maximum)


def validate_execution_id(execution_id: str) -> None:
    require_nonempty(execution_id, "execution_id")
    require_bounded(execution_id, "execution_id", MAX_IDENTIFIER_CHARS)
    if ":" in execution_id:
        raise ValueError("execution_id cannot contain ':'")


def _bounded_reason(reason: str | None) -> None:
    if reason is not None and not 1 <= len(reason) <= MAX_REASON_CODE_CHARS:
        raise ValueError("reason code is invalid")
