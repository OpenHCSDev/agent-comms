"""Private, versioned bus sidebands; public projections must never consume them."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .wire_record import MessageWireRecord
from .field_codec import TextRepresentation
from .errors import RelationViolationError

if TYPE_CHECKING:
    from .audience_manifest import FrozenAudience
    from .delivery_policy import DeliveryManifest, KeyedResponseReceipt
    from .messages import Message
    from .wake import NoWakeDecision, WakeDecision


PRIVATE_WIRE_FIELD: Final = "_agent_comms_private_v1"
_PRIVATE_WIRE_PREFIX: Final = "_agent_comms_private"
INITIAL_CODEC: Final = "audience-v1"
_DECISION_DOMAIN: Final = b"agent-comms:cohort-decisions:v1\0"
_HEX32: Final = re.compile(r"[0-9a-f]{32}\Z")
_HEX64: Final = re.compile(r"[0-9a-f]{64}\Z")


class StableLookupText(TextRepresentation):
    """Private source lookup spelling owned by the original bus publication."""

    @classmethod
    def encode(cls, value: object) -> object:
        return cls.decode(value)

    @classmethod
    def from_text(cls, value: str) -> str:
        if _HEX32.fullmatch(value) is None:
            raise ValueError("Invalid private stable recipient lookup")
        return value


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
        raise ValueError("A keyed wire timestamp must be finite.")
    if has_private_wire_fields(record):
        raise ValueError("Digest input must be a public envelope.")
    return hashlib.sha256(_canonical(record)).hexdigest()


class DuplicateWireKeyError(ValueError):
    """Ambiguous JSON cannot carry a canonical wire receipt."""


def unique_wire_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous JSON objects, including nested receipt keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateWireKeyError("Duplicate bus object key.")
        result[key] = value
    return result


def has_private_wire_fields(record: Mapping[str, object]) -> bool:
    """Identify reserved private keys without inspecting their values."""
    return any(isinstance(key, str) and key.startswith(_PRIVATE_WIRE_PREFIX) for key in record)


def reject_private_wire_fields(record: Mapping[str, object]) -> None:
    """Fail closed before an untyped raw wire record enters a public export."""
    if has_private_wire_fields(record):
        raise ValueError("Bus-private wire fields cannot be exported.")


def stable_thread_lookup(created_at: float) -> str:
    """Immutable creation identity; duplicate timestamps fail at capture.

    The registry preserves Thread.created_at across renames and restarts. Its
    canonical name, aliases, mutable tags, PID, and session attachment are NOT
    lookup identities. An unknown creation time (zero) cannot authorize delivery.
    """
    if isinstance(created_at, bool) or not isinstance(created_at, (float, int)):
        raise ValueError("Thread creation identity is invalid.")
    if not math.isfinite(created_at) or created_at <= 0:
        raise ValueError("Thread creation identity is unavailable.")
    identity = f"agent-comms:thread-creation:v1\0{float(created_at)!r}".encode()
    return hashlib.sha256(identity).hexdigest()[:32]


def _decision_wire(decision: WakeDecision | NoWakeDecision) -> dict[str, str]:
    from .wake import NoWakeDecision, WakeDecision

    if type(decision) is NoWakeDecision:
        return {"recipient_lookup": decision.recipient, "no_wake": decision.reason.value}
    if type(decision) is WakeDecision:
        return {
            "recipient_lookup": decision.recipient,
            "audience": decision.audience.value,
            "wake_mode": decision.wake_mode.declared_name,
        }
    raise TypeError("Only typed, pure full-cohort decisions may be committed.")


def decisions_wire(decisions: tuple[WakeDecision | NoWakeDecision, ...]) -> list[dict[str, str]]:
    return [_decision_wire(decision) for decision in decisions]


def decisions_digest(decisions: list[dict[str, str]]) -> str:
    from .audience_manifest import MAX_AUDIENCE_BYTES

    encoded = _canonical(decisions)
    if len(encoded) > MAX_AUDIENCE_BYTES:
        raise ValueError("Private decision set exceeds its byte bound.")
    return hashlib.sha256(_DECISION_DOMAIN + encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CommittedDelivery(MessageWireRecord):
    """Bus-reader delivery, for both an original and a keyed response."""

    wire_root_id: str
    message: Message
    audience: FrozenAudience
    decisions: tuple[WakeDecision | NoWakeDecision, ...]
    decisions_digest: str
    control: str
    resolver_version: str
    policy_version: str
    manifest_codec: str = INITIAL_CODEC
    receipt: KeyedResponseReceipt | None = None

    def direct_for(self, recipient_lookup: str) -> bool:
        """Direct reply semantics and its original addressed incarnation agree."""
        policy = self.message.response_policy
        return (
            policy.starts_turn
            and policy.separate_turn
            and self.audience.includes_lookup(recipient_lookup)
        )

    def compaction_messages_for(self, recipient_lookup):
        addressed = self.audience.includes_lookup(recipient_lookup)
        authored = (
            self.audience.sender_lookup == recipient_lookup
            and self.message.retains_authored_task
        )
        return self.messages() if addressed or authored else ()

    @classmethod
    def from_wire(cls, record: Mapping[str, object], wire_root_id: str) -> CommittedDelivery:
        from .messages import Message

        return cls.attest(Message.from_committed_wire(record), record, wire_root_id)

    @classmethod
    def attest(
        cls, message: Message, record: Mapping[str, object], wire_root_id: str
    ) -> CommittedDelivery:
        """Validate the declared private policy against the already decoded public message."""
        from agent_comms.coordination_contracts import POLICY_VERSION, RESOLVER_VERSION

        from .audience_manifest import freeze_audience
        from .delivery_policy import DeliveryPolicy
        from .field_codec import FieldCodec

        if set(key for key in record if key.startswith(_PRIVATE_WIRE_PREFIX)) != {
            PRIVATE_WIRE_FIELD
        }:
            raise ValueError("Unknown private bus namespace.")
        private = record[PRIVATE_WIRE_FIELD]
        policy = FieldCodec.decode(DeliveryPolicy, private)
        if _canonical(private) != _canonical(FieldCodec.encode(policy)):
            raise ValueError("Private publication is not canonical.")
        raw = policy.initial
        if raw.wire_root_id != wire_root_id:
            raise ValueError("Delivery belongs to another wire root.")
        if raw.manifest_codec != INITIAL_CODEC:
            raise ValueError("Unsupported delivery manifest codec.")
        if raw.resolver_version != RESOLVER_VERSION or raw.policy_version != POLICY_VERSION:
            raise ValueError("Unsupported delivery decision versions.")
        if not raw.control.supports_initial:
            raise ValueError("Unsupported delivery control issuer.")
        audience = raw.audience
        if _HEX64.fullmatch(audience.source_revision) is None:
            raise ValueError("Delivery source revision is invalid.")
        lookups = (audience.sender_lookup, *(r.recipient_lookup for r in audience.recipients))
        if any(_HEX32.fullmatch(lookup) is None for lookup in lookups):
            raise ValueError("Delivery creator or recipient lookup is invalid.")
        candidate = freeze_audience(
            message,
            audience.recipients,
            audience.source_revision,
            sender_lookup=audience.sender_lookup,
            sender_name=audience.sender_name,
        )
        if audience != candidate:
            raise ValueError("Delivery audience differs from committed public envelope.")
        decisions = policy.resolve(message, candidate, raw.control)
        rows = decisions_wire(decisions)
        if raw.decisions != rows or raw.decisions_digest != decisions_digest(rows):
            raise ValueError("Delivery decisions do not match the full committed N.")
        receipt = policy.require_receipt(wire_root_id, message)
        return cls(
            wire_root_id,
            message,
            candidate,
            decisions,
            raw.decisions_digest,
            raw.control.value,
            RESOLVER_VERSION,
            POLICY_VERSION,
            receipt=receipt,
        )

    def deliveries(self) -> tuple[CommittedDelivery, ...]:
        return (self,)

    def require_admission(self, metadata) -> None:
        """Factory already bound root/audience; deliveries need no retained-public exception."""

    def checkpoint_rows(self, offset: int, length: int):
        from .private_bus_checkpoint import Addressed, DeliverySources, ResponseKeys

        if self.receipt is not None:
            yield ResponseKeys(self.receipt.publication_key)
        yield DeliverySources(self.message.seq, self.message.message_id, offset, length,
                              self.audience.sender_lookup)
        for recipient in self.audience.recipients:
            yield Addressed(recipient.recipient_lookup, self.message.seq)


def initial_sideband(
    wire_root_id: str,
    message: Message,
    audience: FrozenAudience,
    decisions: tuple[WakeDecision | NoWakeDecision, ...],
    *,
    control: str,
) -> DeliveryManifest:
    """Serialize once at the committed-bus boundary; no independent sidecar."""
    from agent_comms.coordination_contracts import POLICY_VERSION, RESOLVER_VERSION

    if len(decisions) != len(audience.recipients):
        raise ValueError("Every frozen recipient requires a selected or no-wake decision.")
    from .delivery_policy import DeliveryManifest
    from .wake import ControlClassification

    rows = decisions_wire(decisions)
    return DeliveryManifest(
        wire_root_id,
        INITIAL_CODEC,
        RESOLVER_VERSION,
        POLICY_VERSION,
        ControlClassification(control),
        audience,
        rows,
        decisions_digest(rows),
    )


@dataclass(frozen=True, slots=True)
class HumanOrigin:
    """Cooperative local UI origin, checked against the registered USER under lock.

    This is not a cryptographic credential against another process of this UID.
    It never grants an executable owner, claim, tool, or selected source authority.
    """

    sender: str
    created_at: float
    worktree: str

    def require_registered(self, snapshot) -> None:
        """Check the determining USER declaration, never a transport or prose role."""
        user = snapshot.threads.get(self.sender)
        if user is None:
            raise RelationViolationError("Local USER origin is no longer registered")
        user.role.require_user()
        if (user.name, user.created_at, user.worktree) != (self.sender, self.created_at, self.worktree):
            raise RelationViolationError("Local USER origin differs from registered identity")
