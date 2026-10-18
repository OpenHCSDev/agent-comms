"""Private, versioned bus sidebands; public projections must never consume them."""

from __future__ import annotations

import hashlib
import json
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import field, dataclass
from typing import TYPE_CHECKING, Final

from .delivery_policy import DeliveryPolicy
from .messages import Message
from .wake_policy import RecipientDecision
from .wire_record import MessageWireRecord
from .field_codec import FieldCodec, MalformedRecord, TextRepresentation
from .errors import HumanInitialUnknownError, RelationViolationError

if TYPE_CHECKING:
    from .audience_manifest import FrozenAudience
    from .delivery_policy import DeliveryManifest, KeyedResponseAcknowledgement
    from .private_bus_checkpoint import CertifiedSourceRead
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .wire_log import WireLog
    from .wire_metadata import WireMetadata


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
        raise MalformedRecord("A keyed wire timestamp must be finite.")
    if has_private_wire_fields(record):
        raise MalformedRecord("Digest input must be a public envelope.")
    return hashlib.sha256(_canonical(record)).hexdigest()


def has_private_wire_fields(record: Mapping[str, object]) -> bool:
    """Identify reserved private keys without inspecting their values."""
    return any(isinstance(key, str) and key.startswith(_PRIVATE_WIRE_PREFIX) for key in record)


def reject_private_wire_fields(record: Mapping[str, object]) -> None:
    """Fail closed before an untyped raw wire record enters a public export."""
    if has_private_wire_fields(record):
        raise ValueError("Bus-private wire fields cannot be exported.")


def decisions_wire(decisions: tuple[RecipientDecision, ...]) -> list[dict[str, str]]:
    return [decision.to_wire() for decision in decisions]


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
    decisions: tuple[RecipientDecision, ...]
    decisions_digest: str
    control: str
    resolver_version: str
    policy_version: str
    manifest_codec: str = INITIAL_CODEC
    acknowledgement: KeyedResponseAcknowledgement | None = field(default=None, metadata={"wire_name": 'receipt'})

    def direct_for(self, recipient_lookup: str) -> bool:
        """Direct reply semantics and its original addressed incarnation agree."""
        policy = self.message.role.response_policy(self.message)
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
    def attest(
        cls, message: Message, record: Mapping[str, object], wire_root_id: str
    ) -> CommittedDelivery:
        """Validate the declared private policy against the already decoded public message."""
        from agent_comms.coordination_contracts import POLICY_VERSION, RESOLVER_VERSION

        from .audience_manifest import freeze_audience

        if set(key for key in record if key.startswith(_PRIVATE_WIRE_PREFIX)) != {
            PRIVATE_WIRE_FIELD
        }:
            raise MalformedRecord("Unknown private bus namespace.")
        private = record[PRIVATE_WIRE_FIELD]
        policy = DeliveryPolicy.from_sideband(private)
        if _canonical(private) != _canonical(FieldCodec.encode(policy)):
            raise MalformedRecord("Private publication is not canonical.")
        raw = policy.initial
        if raw.wire_root_id != wire_root_id:
            raise MalformedRecord("Delivery belongs to another wire root.")
        if raw.manifest_codec != INITIAL_CODEC:
            raise MalformedRecord("Unsupported delivery manifest codec.")
        if raw.resolver_version != RESOLVER_VERSION or raw.policy_version != POLICY_VERSION:
            raise MalformedRecord("Unsupported delivery decision versions.")
        if not raw.control.supports_initial:
            raise MalformedRecord("Unsupported delivery control issuer.")
        audience = raw.audience
        if _HEX64.fullmatch(audience.source_revision) is None:
            raise MalformedRecord("Delivery source revision is invalid.")
        lookups = (audience.sender_lookup, *(r.recipient_lookup for r in audience.recipients))
        if any(_HEX32.fullmatch(lookup) is None for lookup in lookups):
            raise MalformedRecord("Delivery creator or recipient lookup is invalid.")
        candidate = freeze_audience(
            message,
            audience.recipients,
            audience.source_revision,
            sender_lookup=audience.sender_lookup,
            sender_name=audience.sender_name,
        )
        if audience != candidate:
            raise MalformedRecord("Delivery audience differs from committed public envelope.")
        decisions = policy.resolve(message, candidate, raw.control)
        rows = decisions_wire(decisions)
        if raw.decisions != rows or raw.decisions_digest != decisions_digest(rows):
            raise MalformedRecord("Delivery decisions do not match the full committed N.")
        acknowledgement = policy.require_acknowledgement(wire_root_id, message)
        return cls.proved(wire_root_id, message, policy, decisions, acknowledgement)

    @classmethod
    def recorded(cls, record: Mapping[str, object], wire_root_id: str) -> CommittedDelivery:
        """A row the private bus row store indexed, so attest proved it when it entered."""
        policy = DeliveryPolicy.from_sideband(record[PRIVATE_WIRE_FIELD])
        decisions = tuple(map(RecipientDecision.from_wire, policy.initial.decisions))
        message = Message.from_committed_wire(record)
        return cls.proved(wire_root_id, message, policy, decisions, policy.acknowledgement)

    @classmethod
    def proved(
        cls, wire_root_id: str, message: Message, policy: DeliveryPolicy,
        decisions: tuple[RecipientDecision, ...],
        acknowledgement: KeyedResponseAcknowledgement | None,
    ) -> CommittedDelivery:
        """The delivery a proved policy declares for its message."""
        raw = policy.initial
        return cls(
            wire_root_id,
            message,
            raw.audience,
            decisions,
            raw.decisions_digest,
            raw.control.value,
            raw.resolver_version,
            raw.policy_version,
            acknowledgement=acknowledgement,
        )

    def deliveries(self) -> tuple[CommittedDelivery, ...]:
        return (self,)

    def require_claim(self, metadata) -> None:
        """Factory already bound root/audience; deliveries need no retained-public exception."""

    def checkpoint_rows(self, offset: int, length: int, previous_sequence: int):
        from .private_bus_checkpoint import Addressed, DeliverySources, Replies, ResponseKeys

        yield from super().checkpoint_rows(offset, length, previous_sequence)
        if self.acknowledgement is not None:
            yield ResponseKeys(self.acknowledgement.publication_key)
        yield DeliverySources(self.message.seq, self.message.message_id,
                              self.audience.sender_lookup, offset=offset, length=length)
        for recipient in self.audience.recipients:
            yield Addressed(recipient.recipient_lookup, self.message.seq)
        for answered in self.message.answers:
            yield Replies(answered.seq, self.message.seq)

    def delivery_messages(self):
        from .routing import DeliveryMessage

        if self.message.type.conversation_message:
            return (DeliveryMessage(self.message, self.audience.sender_lookup),)
        return ()


def initial_sideband(
    wire_root_id: str,
    message: Message,
    audience: FrozenAudience,
    decisions: tuple[RecipientDecision, ...],
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


class InitialOrigin(ABC):
    """Who sends an initial message, checked under the bus lock before its append."""

    __slots__ = ()

    @abstractmethod
    def require_sender(
        self, message: Message, sender_thread: Thread, snapshot: RegistrySnapshot
    ) -> None:
        """Refuse a sender this origin cannot speak for."""

    @abstractmethod
    def require_source(self, source: CertifiedSourceRead, sender: str, message_id: str) -> None:
        """What the certified source must show before the append."""

    @abstractmethod
    def append(
        self, log: WireLog, metadata: WireMetadata, row: Mapping[str, object], stored: Message
    ) -> None:
        """Append the reserved row, reporting a failure as this origin requires."""


@dataclass(frozen=True, slots=True)
class AgentOrigin(InitialOrigin):
    """An executable thread's owner; it may resend under the same message id."""

    def require_sender(
        self, message: Message, sender_thread: Thread, snapshot: RegistrySnapshot
    ) -> None:
        sender_thread.role.require_executable(
            RelationViolationError("Initial sender must be a visible registered executable.")
        )

    def require_source(self, source: CertifiedSourceRead, sender: str, message_id: str) -> None:
        """An agent's resend is idempotent, so the source holds no precondition."""

    def append(
        self, log: WireLog, metadata: WireMetadata, row: Mapping[str, object], stored: Message
    ) -> None:
        log.append_private_unlocked(metadata, row)


@dataclass(frozen=True, slots=True)
class HumanOrigin(InitialOrigin):
    """Cooperative local UI origin, checked against the registered USER under lock.

    This is not a cryptographic credential against another process of this UID.
    It never grants an executable owner, claim, tool, or selected source authority.
    """

    sender: str
    created_at: float
    worktree: str

    def require_sender(
        self, message: Message, sender_thread: Thread, snapshot: RegistrySnapshot
    ) -> None:
        self.require_registered(snapshot)
        if message.sender != self.sender:
            raise RelationViolationError("Local USER origin differs from registered identity.")

    def require_source(self, source: CertifiedSourceRead, sender: str, message_id: str) -> None:
        source.require_new_human_message(sender, message_id)

    def append(
        self, log: WireLog, metadata: WireMetadata, row: Mapping[str, object], stored: Message
    ) -> None:
        try:
            log.append_private_unlocked(metadata, row)
        except BaseException as error:
            # Even cancellation/interrupt after entry can follow a durable
            # reservation or row. Never claim absence or retry this ID.
            raise HumanInitialUnknownError(
                metadata.root_id, stored.seq, stored.message_id
            ) from error

    def require_registered(self, snapshot: RegistrySnapshot) -> None:
        """Check the determining USER declaration, never a transport or prose role."""
        user = snapshot.threads.get(self.sender)
        if user is None:
            raise RelationViolationError("Local USER origin is no longer registered")
        user.role.require_user(
            RelationViolationError("A user correction requires the original human sender")
        )
        registered = (user.name, user.created_at, user.worktree)
        if registered != (self.sender, self.created_at, self.worktree):
            raise RelationViolationError("Local USER origin differs from registered identity")
