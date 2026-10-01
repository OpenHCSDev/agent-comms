"""Messages: declaration and persistence owners."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from enum import Enum, StrEnum
from typing import TYPE_CHECKING

from .channel_targets import _TAG_CHARS, BuiltinChannel, is_channel_target
from .envelope_claim_transitions import ClaimTransition
from .errors import RelationViolationError
from .field_codec import FieldCodec, projected
from .mentions import ThreadMention
from .message_reference import MessageReference
from .response_policy import (
    CollectivePolicy,
    DirectPolicy,
    InformationalPolicy,
    MentionedOnlyPolicy,
    ResponseEligibility,
    ResponsePolicy,
)
from .thread_identity import ThreadRole
from .task_decisions import Decision, DecisionAttachment, NoDecision

if TYPE_CHECKING:
    from .historical_views import HistoryCursor


class MessageType(Enum):
    INFO = "info"
    QUESTION = "question"
    ACK = "ack"
    HANDOFF = "handoff"
    ALERT = "alert"


class MembershipChange(StrEnum):
    JOINED = "joined"
    LEFT = "left"


@dataclass(frozen=True, slots=True)
class Message:
    """Declares one inter-thread message.

    The bus assigns ``seq`` (a monotonically increasing log position) at send
    time; the hash-derived ``message_id`` is identity only and is not ordered.
    """

    sender: str = field(metadata={"wire_name": "from", "wire_order": 2})
    target: str = field(metadata={"wire_name": "to", "wire_order": 3})
    body: str = field(metadata={"wire_name": "text", "wire_order": 6})
    type: MessageType = field(metadata={"wire_order": 5})
    timestamp: float = field(
        default_factory=time.time, metadata={"wire_name": "ts", "wire_order": 4}
    )
    seq: int = field(default=0, metadata={"wire_order": 0, "publication_exclude": True})
    sender_role: ThreadRole = field(
        default=ThreadRole.AGENT, metadata={"wire_order": 7, "publication_exclude": True}
    )
    membership: MembershipChange | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_order": 8}
    )
    notice: bool = field(default=False, metadata={"wire_omit_default": True, "wire_order": 9})
    mentions: tuple[ThreadMention, ...] = field(
        default=(),
        metadata={"wire_omit_default": True, "wire_order": 10, "publication_exclude": True},
    )
    claim_transition: ClaimTransition | None = field(
        default=None,
        metadata={"wire_omit_default": True, "wire_order": 11, "publication_exclude": True},
    )
    decision: DecisionAttachment = field(
        default_factory=NoDecision, metadata={"wire_omit_default": True, "wire_order": 12}
    )

    @property
    def publication_snapshot(self) -> Message:
        """Frozen publication content before independently owned delivery enrichment.

        Sequence, role, resolved mentions and claim evidence are assigned by the
        bus. Their declarations exclude them from publication content identity;
        all other declared fields participate in the Message value comparison.
        This is an in-memory snapshot, never a different wire representation.
        """
        return replace(
            self,
            **{
                declared.name: declared.default
                for declared in fields(self)
                if declared.metadata.get("publication_exclude")
            },
        )

    @property
    def reference(self) -> MessageReference:
        return MessageReference(self.seq, self.message_id)

    def notification_references(self) -> tuple[MessageReference, ...]:
        """This live original may read current handling through its exact source."""
        return (self.reference,)

    def require_decision(self) -> Decision:
        return self.decision.require_decision()

    @property
    def retains_authored_choice(self) -> bool:
        return self.decision.retains_authored_choice

    def require_task_publication(self, sender, snapshot, original_source) -> None:
        self.decision.require_publication(sender, snapshot, original_source)

    def require_claim_transition(self) -> ClaimTransition:
        if self.claim_transition is None:
            raise RelationViolationError("Original wire message has no claim transition")
        return self.claim_transition

    def retained_task_facts(self):
        from .retained_task_facts import (
            ClaimTaskFact,
        )

        if self.sender_role is ThreadRole.USER:
            yield from self.decision.user_task_facts(self)
        yield from self.decision.retained_task_facts(self)
        if self.claim_transition is not None:
            yield ClaimTaskFact(self)

    def __post_init__(self) -> None:
        if not self.sender:
            raise RelationViolationError("Message sender cannot be empty.")
        if not self.target:
            raise RelationViolationError("Message target cannot be empty.")
        if not self.body:
            raise ValueError("Message body cannot be empty.")
        self.decision.require_sender(self.sender)
        if self.claim_transition is not None:
            transition = self.claim_transition
            if type(transition) is not ClaimTransition or (
                self.seq > 0
                and (
                    transition.owner != self.sender
                    or transition.seq != self.seq
                    or transition.message_id != self.message_id
                )
            ):
                raise RelationViolationError("Claim transition does not bind this message.")
        if any(
            not (0 <= mention.start < mention.end <= len(self.body))
            or self.body[mention.start] != "@"
            for mention in self.mentions
        ):
            raise ValueError("Mention ranges must identify text in the message body.")
        if not is_channel_target(self.target):
            # DM: a thread name; sending to yourself is not a conversation.
            if self.sender == self.target:
                raise RelationViolationError(f"Thread {self.sender!r} cannot message itself.")
            allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
            if not set(self.target) <= allowed:
                raise ValueError(
                    f"Message target {self.target!r} is not a thread name or #channel."
                )
        if self.target.startswith("#") and self.target != BuiltinChannel.ALL.value:
            tag = self.target[1:]
            if not tag or not set(tag) <= _TAG_CHARS:
                raise ValueError(
                    f"Channel {self.target!r} must be lowercase alphanumeric"
                    " with hyphens/underscores."
                )

    @property
    def view_cursor(self) -> int | HistoryCursor:
        return self.seq

    @property
    def view_key(self) -> tuple[str, int]:
        return "", self.seq

    @property
    def view_order(self) -> tuple[int, int, int]:
        return 1, 0, self.seq

    @projected(view="wire", name="id")
    def message_id(self) -> str:
        digest = hashlib.sha256(
            f"{self.sender}:{self.target}:{self.timestamp}:{self.body}".encode()
        ).hexdigest()[:12]
        return digest

    @property
    def display_metadata(self) -> dict:
        """Live messages carry no historical provenance decoration."""
        return {}

    def to_wire(self) -> dict:
        return {"seq": self.seq, "id": self.message_id, **FieldCodec.encode(self)}

    @classmethod
    def from_wire(cls, data: Mapping) -> Message:
        from .bus_publication import PRIVATE_WIRE_FIELD

        # ID is derived; private authority is independently validated by the bus.
        public = {
            key: value for key, value in data.items() if key not in ("id", PRIVATE_WIRE_FIELD)
        }
        return FieldCodec.decode(cls, {"ts": 0.0, **public})

    @classmethod
    def from_committed_wire(cls, record: Mapping) -> Message:
        """One strict public-envelope boundary for full, indexed and cohort readers."""
        from .audience_manifest import MAX_WIRE_SEQ
        from .bus_publication import PRIVATE_WIRE_FIELD, _canonical, public_envelope_digest

        if not isinstance(record, Mapping):
            raise ValueError("Bus row is not an object.")
        public = {key: value for key, value in record.items() if key != PRIVATE_WIRE_FIELD}
        message = cls.from_wire(public)
        if _canonical(public) != _canonical(message.to_wire()):
            raise ValueError("Noncanonical public bus envelope.")
        if not 0 < message.seq <= MAX_WIRE_SEQ:
            raise ValueError("Bus sequence is outside the durable range.")
        public_envelope_digest(public)
        return message

    def require_retained_admission(self, admission_after_seq: int) -> None:
        if self.claim_transition is None and self.seq > admission_after_seq:
            raise RelationViolationError(
                "Unattested public initial exceeds the retained history boundary."
            )

    @property
    def response_policy(self) -> ResponsePolicy:
        """Typed response semantics without changing the stored target."""
        if self.notice or self.membership is not None:
            return InformationalPolicy.instance()
        if not (is_channel_target(self.target) or BuiltinChannel.lookup(self.target) is not None):
            return DirectPolicy.instance()
        if self.mentions:
            return MentionedOnlyPolicy.instance()
        if not self.sender_role.executable:
            return CollectivePolicy.instance()
        return InformationalPolicy.instance()

    def response_eligibility(
        self, audience: Sequence[str], *, aliases: Mapping[str, str] | None = None
    ) -> ResponseEligibility:
        """Resolve channel responders from canonical audience identities.

        Mention identities were canonicalized when the message was committed.
        The audience remains authoritative for membership, so a valid mention
        outside the channel cannot acquire delivery by being named.
        """
        policy = self.response_policy
        return ResponseEligibility(policy, policy.recipients(self, audience, aliases=aliases))

    @property
    def starts_turn(self) -> bool:
        """Whether every delivered recipient may start a turn."""
        return self.response_policy.starts_turn

    def starts_turn_for(self, name: str, *, aliases: Mapping[str, str] | None = None) -> bool:
        """Whether this message enters one canonical recipient's model context."""
        channel = is_channel_target(self.target) or BuiltinChannel.lookup(self.target) is not None
        if not channel:
            return self.starts_turn
        return name in self.response_eligibility((name,), aliases=aliases).recipients

    @property
    def reply_target(self) -> str | None:
        if self.sender_role.executable:
            return None
        return self.target if is_channel_target(self.target) else self.sender
