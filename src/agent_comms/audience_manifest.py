"""Pure, precommit audience values; no membership, wake, or delivery authority.

The caller captures the complete eligible recipient set at the original wire
boundary. A frozen value is not evidence that a bus row or sideband was fsynced.
The bus integrator must attest target routability (including saved-view rejection)
and bind the value to the original committed envelope before runtime use.
"""

from __future__ import annotations

from .bus_tables import (
    AwaitingAnswerSource,
    ReceivedSource,
)

from .class_fact import ClassFact
import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Final

from agent_comms.coordination_contracts import MAX_IDENTIFIER_CHARS

from .channel_targets import exact_stored_target
from .field_codec import FieldCodec, MalformedRecord, ThreadLookup
from .messages import BusCapability, CarriesEnvelope, Message
from .projection_source import ProducedRow, ProjectionSource

AUDIENCE_VERSION: Final = 1
MAX_RECIPIENTS: Final = 4096
MAX_WIRE_ENVELOPE_BYTES: Final = 4 * 1024 * 1024
MAX_AUDIENCE_BYTES: Final = 1024 * 1024
MAX_WIRE_SEQ: Final = (1 << 63) - 1
_ENVELOPE_DOMAIN: Final = b"agent-comms:wire-envelope:v1\0"
_AUDIENCE_DOMAIN: Final = b"agent-comms:audience:v1\0"
_NAME: Final = re.compile(r"[a-zA-Z0-9_-]+\Z")


def _bounded_text(value: str, field: str, *, thread_name: bool = False) -> None:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_CHARS:
        raise MalformedRecord(f"{field} must be a bounded nonempty string")
    if thread_name and _NAME.fullmatch(value) is None:
        raise MalformedRecord(f"{field} must be a canonical thread name")
    if not thread_name and not value.strip():
        raise MalformedRecord(f"{field} must not be whitespace")


def _canonical_json(value: object, *, limit: int) -> bytes:
    try:
        encoded = FieldCodec.compact(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except ValueError as error:  # NaN, or a lone surrogate the UTF-8 encoding refuses
        raise MalformedRecord("audience data cannot be canonically encoded") from error
    if len(encoded) > limit:
        raise MalformedRecord("audience data exceeds its byte limit")
    return encoded


def _digest(domain: bytes, payload: bytes) -> str:
    return hashlib.sha256(domain + payload).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenRecipient(ProjectionSource):
    """Stable coordination lookup paired with its send-time canonical name."""

    @ClassFact
    def writes(cls) -> tuple[type[ProducedRow], ...]:
        return (ReceivedSource, AwaitingAnswerSource)


    recipient_lookup: ThreadLookup
    canonical_thread: str

    def __post_init__(self) -> None:
        _bounded_text(self.recipient_lookup, "recipient_lookup")
        _bounded_text(self.canonical_thread, "canonical_thread", thread_name=True)


@dataclass(frozen=True, kw_only=True)
class Audience(BusCapability):
    """The participant facts frozen at publication."""

    sender_lookup: ThreadLookup
    source_revision: str
    recipients: tuple[FrozenRecipient, ...]

    @property
    def canonical_members(self) -> frozenset[str]:
        """Names supplied to the separate pure wake resolver; no membership lookup."""
        return frozenset(item.canonical_thread for item in self.recipients)

    def includes_lookup(self, lookup: ThreadLookup) -> bool:
        """Membership in the original frozen audience, independent of current names/tags."""
        return any(item.recipient_lookup == lookup for item in self.recipients)


@dataclass(frozen=True, kw_only=True)
class AudiencedMessage(CarriesEnvelope, Message, Audience):
    """A prepared message and its frozen participants, with one content identity."""

    def __post_init__(self) -> None:
        super().__post_init__()
        if type(self.seq) is not int or not 0 < self.seq <= MAX_WIRE_SEQ:
            raise MalformedRecord("wire_seq must be a positive SQLite-range integer")
        for name in ("target", "source_revision"):
            _bounded_text(getattr(self, name), name)
        _bounded_text(self.sender, "sender_name", thread_name=True)
        if not exact_stored_target(self.target):
            raise MalformedRecord("aggregate views are not exact stored targets")
        if type(self.recipients) is not tuple or len(self.recipients) > MAX_RECIPIENTS:
            raise MalformedRecord("recipients must be a bounded immutable tuple")
        sorted_recipients = tuple(sorted(self.recipients, key=lambda item: item.recipient_lookup))
        if sorted_recipients != self.recipients:
            raise MalformedRecord("recipients must be ordered by stable lookup")
        lookups = [item.recipient_lookup for item in self.recipients]
        names = [item.canonical_thread for item in self.recipients]
        if len(set(lookups)) != len(lookups) or len(set(names)) != len(names):
            raise MalformedRecord("frozen recipient lookups and canonical names must be unique")
        if self.sender_lookup in lookups or self.sender in names:
            raise MalformedRecord("the sender cannot be an eligible recipient")
        if not math.isfinite(self.timestamp):
            raise MalformedRecord(
                "audience data cannot be canonically encoded: non-finite timestamp"
            )

    @cached_property
    def wire_envelope_digest(self) -> str:
        return _digest(
            _ENVELOPE_DOMAIN, _canonical_json(self.to_wire(), limit=MAX_WIRE_ENVELOPE_BYTES)
        )

    @cached_property
    def digest(self) -> str:
        return _digest(
            _AUDIENCE_DOMAIN, _canonical_json(self._digest_record(), limit=MAX_AUDIENCE_BYTES)
        )

    def _digest_record(self) -> dict[str, object]:
        return {
            "version": AUDIENCE_VERSION,
            "wire_seq": self.seq,
            "message_id": self.message_id,
            "exact_target": self.target,
            "sender_lookup": self.sender_lookup,
            "sender_name": self.sender,
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


def freeze_audience(
    prepared_message: Message,
    externally_captured_recipients: Sequence[FrozenRecipient],
    source_revision: str,
    *,
    sender_lookup: ThreadLookup,
    sender_name: str,
) -> AudiencedMessage:
    """Build a precommit value from an externally captured complete recipient set.

    Mentions do not filter this set. Neither current tags nor view declarations are
    read here. A sequence assigned in memory is not proof of durable commitment.
    """
    if not isinstance(externally_captured_recipients, Sequence) or isinstance(
        externally_captured_recipients, (str, bytes)
    ):
        raise TypeError("externally_captured_recipients must be a bounded sequence")
    if len(externally_captured_recipients) > MAX_RECIPIENTS:
        raise MalformedRecord("too many frozen recipients")
    _bounded_text(sender_name, "sender_name", thread_name=True)
    if prepared_message.sender != sender_name:
        raise MalformedRecord(
            "sender_name must be the externally captured canonical message sender"
        )
    audience = AudiencedMessage.from_message(
        prepared_message,
        sender_lookup=sender_lookup,
        source_revision=source_revision,
        recipients=tuple(sorted(
            externally_captured_recipients, key=lambda item: item.recipient_lookup
        )),
    )
    # Publication enforces the canonical byte bounds; history reads do not rehash.
    _ = audience.digest
    return audience
