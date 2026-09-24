"""Pure, precommit audience values; no membership, wake, or delivery authority.

The caller captures the complete eligible recipient set at the original wire
boundary. A frozen value is not evidence that a bus row or sideband was fsynced.
The bus integrator must attest target routability (including saved-view rejection)
and bind the value to the original committed envelope before runtime use.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from .coordination import MAX_IDENTIFIER_CHARS
from .declarations import Message

AUDIENCE_VERSION: Final = 1
MAX_RECIPIENTS: Final = 4096
MAX_WIRE_ENVELOPE_BYTES: Final = 4 * 1024 * 1024
MAX_AUDIENCE_BYTES: Final = 1024 * 1024
MAX_WIRE_SEQ: Final = (1 << 63) - 1
_ENVELOPE_DOMAIN: Final = b"agent-comms:wire-envelope:v1\0"
_AUDIENCE_DOMAIN: Final = b"agent-comms:audience:v1\0"
_NAME: Final = re.compile(r"[a-zA-Z0-9_-]+\Z")
_HEX_DIGEST: Final = re.compile(r"[0-9a-f]{64}\Z")


def _bounded_text(value: str, field: str, *, thread_name: bool = False) -> None:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_CHARS:
        raise ValueError(f"{field} must be a bounded nonempty string")
    if thread_name and _NAME.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical thread name")
    if not thread_name and not value.strip():
        raise ValueError(f"{field} must not be whitespace")


def _canonical_json(value: object, *, limit: int) -> bytes:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("audience data cannot be canonically encoded") from error
    if len(encoded) > limit:
        raise ValueError("audience data exceeds its byte limit")
    return encoded


def _digest(domain: bytes, payload: bytes) -> str:
    return hashlib.sha256(domain + payload).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenRecipient:
    """Stable coordination lookup paired with its send-time canonical name."""

    recipient_lookup: str
    canonical_thread: str

    def __post_init__(self) -> None:
        _bounded_text(self.recipient_lookup, "recipient_lookup")
        _bounded_text(self.canonical_thread, "canonical_thread", thread_name=True)


@dataclass(frozen=True, slots=True)
class FrozenAudience:
    """Immutable precommit snapshot of the full delivered audience, not a receipt.

    Neither the digest nor direct construction attests a bus append. At recovery,
    the bus owner must match this value to the original wire envelope/sideband.
    """

    wire_seq: int
    message_id: str
    exact_target: str
    sender_lookup: str
    sender_name: str
    source_revision: str
    recipients: tuple[FrozenRecipient, ...]
    wire_envelope_digest: str
    digest: str
    version: int = AUDIENCE_VERSION

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != AUDIENCE_VERSION:
            raise ValueError("unsupported audience version")
        if type(self.wire_seq) is not int or not 0 < self.wire_seq <= MAX_WIRE_SEQ:
            raise ValueError("wire_seq must be a positive SQLite-range integer")
        for name in ("message_id", "exact_target", "sender_lookup", "source_revision"):
            _bounded_text(getattr(self, name), name)
        _bounded_text(self.sender_name, "sender_name", thread_name=True)
        if self.exact_target in {"#any", "broadcast"}:
            raise ValueError("aggregate views and broadcast aliases are not exact stored targets")
        if type(self.recipients) is not tuple or len(self.recipients) > MAX_RECIPIENTS:
            raise ValueError("recipients must be a bounded immutable tuple")
        if any(type(item) is not FrozenRecipient for item in self.recipients):
            raise TypeError("recipients must contain FrozenRecipient values")
        sorted_recipients = tuple(sorted(self.recipients, key=lambda item: item.recipient_lookup))
        if sorted_recipients != self.recipients:
            raise ValueError("recipients must be ordered by stable lookup")
        lookups = [item.recipient_lookup for item in self.recipients]
        names = [item.canonical_thread for item in self.recipients]
        if len(set(lookups)) != len(lookups) or len(set(names)) != len(names):
            raise ValueError("frozen recipient lookups and canonical names must be unique")
        if self.sender_lookup in lookups or self.sender_name in names:
            raise ValueError("the sender cannot be an eligible recipient")
        if not isinstance(self.wire_envelope_digest, str) or not _HEX_DIGEST.fullmatch(
            self.wire_envelope_digest
        ):
            raise ValueError("wire_envelope_digest must be lowercase SHA-256 hex")
        if not isinstance(self.digest, str) or not _HEX_DIGEST.fullmatch(self.digest):
            raise ValueError("digest must be lowercase SHA-256 hex")
        if self.digest != _digest(
            _AUDIENCE_DOMAIN, _canonical_json(self._digest_record(), limit=MAX_AUDIENCE_BYTES)
        ):
            raise ValueError("audience digest does not match the frozen value")

    def _digest_record(self) -> dict[str, object]:
        return {
            "version": self.version,
            "wire_seq": self.wire_seq,
            "message_id": self.message_id,
            "exact_target": self.exact_target,
            "sender_lookup": self.sender_lookup,
            "sender_name": self.sender_name,
            "source_revision": self.source_revision,
            "recipients": [
                {
                    "recipient_lookup": item.recipient_lookup,
                    "canonical_thread": item.canonical_thread,
                }
                for item in self.recipients
            ],
            "wire_envelope_digest": self.wire_envelope_digest,
        }

    @property
    def canonical_members(self) -> frozenset[str]:
        """Names supplied to the separate pure wake resolver; no membership lookup."""
        return frozenset(item.canonical_thread for item in self.recipients)


def freeze_audience(
    prepared_message: Message,
    externally_captured_recipients: Sequence[FrozenRecipient],
    source_revision: str,
    *,
    sender_lookup: str,
    sender_name: str,
) -> FrozenAudience:
    """Build a precommit value from an externally captured complete recipient set.

    Mentions do not filter this set. Neither current tags nor view declarations are
    read here. A sequence assigned in memory is not proof of durable commitment.
    """
    if not isinstance(prepared_message, Message):
        raise TypeError("prepared_message must be a Message")
    if not isinstance(externally_captured_recipients, Sequence) or isinstance(
        externally_captured_recipients, (str, bytes)
    ):
        raise TypeError("externally_captured_recipients must be a bounded sequence")
    if len(externally_captured_recipients) > MAX_RECIPIENTS:
        raise ValueError("too many frozen recipients")
    if any(type(item) is not FrozenRecipient for item in externally_captured_recipients):
        raise TypeError("externally_captured_recipients must contain FrozenRecipient values")
    _bounded_text(sender_lookup, "sender_lookup")
    _bounded_text(sender_name, "sender_name", thread_name=True)
    if prepared_message.sender != sender_name:
        raise ValueError("sender_name must be the externally captured canonical message sender")
    _bounded_text(source_revision, "source_revision")
    if prepared_message.target in {"#any", "broadcast"}:
        raise ValueError("only exact stored routable targets may be frozen")
    if type(prepared_message.seq) is not int or not 0 < prepared_message.seq <= MAX_WIRE_SEQ:
        raise ValueError("the prepared message must have an assigned wire sequence")
    wire = _canonical_json(prepared_message.to_wire(), limit=MAX_WIRE_ENVELOPE_BYTES)
    recipients = tuple(
        sorted(externally_captured_recipients, key=lambda item: item.recipient_lookup)
    )
    envelope_digest = _digest(_ENVELOPE_DOMAIN, wire)
    record = {
        "version": AUDIENCE_VERSION,
        "wire_seq": prepared_message.seq,
        "message_id": prepared_message.message_id,
        "exact_target": prepared_message.target,
        "sender_lookup": sender_lookup,
        "sender_name": sender_name,
        "source_revision": source_revision,
        "recipients": [
            {
                "recipient_lookup": item.recipient_lookup,
                "canonical_thread": item.canonical_thread,
            }
            for item in recipients
        ],
        "wire_envelope_digest": envelope_digest,
    }
    return FrozenAudience(
        wire_seq=prepared_message.seq,
        message_id=prepared_message.message_id,
        exact_target=prepared_message.target,
        sender_lookup=sender_lookup,
        sender_name=sender_name,
        source_revision=source_revision,
        recipients=recipients,
        wire_envelope_digest=envelope_digest,
        digest=_digest(_AUDIENCE_DOMAIN, _canonical_json(record, limit=MAX_AUDIENCE_BYTES)),
    )
