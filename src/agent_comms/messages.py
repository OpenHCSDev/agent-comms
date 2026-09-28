"""Messages: declaration and persistence owners."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any

from .channel_targets import _TAG_CHARS, BuiltinChannel, is_channel_target
from .envelope_claim_transitions import (
    ClaimTransition,
    _claim_transition_from_wire,
    _claim_transition_wire,
)
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .mentions import ThreadMention
from .response_policy import (
    CollectivePolicy,
    DirectPolicy,
    InformationalPolicy,
    MentionedOnlyPolicy,
    ResponseEligibility,
    ResponsePolicy,
)
from .thread_identity import ThreadRole

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


class MessageWireCodec(FieldCodec):
    """Use the claim transition boundary inside the standard message codec."""

    @classmethod
    def _decode(cls, target: Any, data: Any) -> Any:
        if target is ClaimTransition:
            return _claim_transition_from_wire(data)
        return super()._decode(target, data)


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
    seq: int = field(default=0, metadata={"wire_order": 0})
    sender_role: ThreadRole = field(default=ThreadRole.AGENT, metadata={"wire_order": 7})
    membership: MembershipChange | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_order": 8}
    )
    notice: bool = field(default=False, metadata={"wire_omit_default": True, "wire_order": 9})
    mentions: tuple[ThreadMention, ...] = field(
        default=(), metadata={"wire_omit_default": True, "wire_order": 10}
    )
    claim_transition: ClaimTransition | None = field(
        default=None, metadata={"wire_omit_default": True, "wire_order": 11}
    )

    def __post_init__(self) -> None:
        if not self.sender:
            raise RelationViolationError("Message sender cannot be empty.")
        if not self.target:
            raise RelationViolationError("Message target cannot be empty.")
        if not self.body:
            raise ValueError("Message body cannot be empty.")
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

    @property
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
        result = MessageWireCodec.encode(self)
        result = {"seq": self.seq, "id": self.message_id, **result}
        if self.claim_transition is not None:
            result["claim_transition"] = _claim_transition_wire(self.claim_transition)
        return result

    @classmethod
    def from_wire(cls, data: Mapping) -> Message:
        from .bus_publication import PRIVATE_WIRE_FIELD

        # ID is derived; private authority is independently validated by the bus.
        public = {
            key: value for key, value in data.items() if key not in ("id", PRIVATE_WIRE_FIELD)
        }
        return MessageWireCodec.decode(cls, {"ts": 0.0, **public})

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
