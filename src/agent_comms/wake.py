"""Pure shadow decisions for a message and its externally frozen audience.

This module does not choose channel members, accept work, start a turn, or publish
responses. The audience owner captures both canonical names and stable lookups;
a later bus-owned boundary must attest its durable commit before live use.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum


from .audience_manifest import FrozenAudience, FrozenRecipient, freeze_audience
from .channel_targets import CommandTarget, NamedTarget
from .messages import Message
from .wake_policy import MessageAudience, PassiveWake, WakePolicy


class ControlClassification(StrEnum):
    """Explicit system-control classification; not inferred from text or ACK type."""

    ORDINARY = "ordinary"
    SYSTEM_CONTROL = "system_control"

    @property
    def passive(self) -> bool:
        return self is self.SYSTEM_CONTROL

    @property
    def supports_initial(self) -> bool:
        return self is self.ORDINARY


class NoWakeReason(StrEnum):
    UNMENTIONED_OBSERVER = "unmentioned_observer"


@dataclass(frozen=True, slots=True)
class RecipientDecision(ABC):
    recipient: str  # Stable recipient_lookup, never the mutable canonical name.

    @abstractmethod
    def wake_policy(self) -> WakePolicy: ...

    @abstractmethod
    def claim_decisions(self) -> tuple[WakeDecision, ...]: ...

    @abstractmethod
    def to_wire(self) -> dict[str, str]: ...

    def initial_notification(self, recipient, *, observation):
        return self.wake_policy().initial_state()().notification(
            recipient, observation=observation)


@dataclass(frozen=True, slots=True)
class NoWakeDecision(RecipientDecision):
    """A frozen delivered member with no recipient-specific wake or claim."""

    reason: NoWakeReason = NoWakeReason.UNMENTIONED_OBSERVER

    def wake_policy(self) -> WakePolicy:
        return PassiveWake()

    def claim_decisions(self) -> tuple[WakeDecision, ...]:
        return ()

    def to_wire(self) -> dict[str, str]:
        return {"recipient_lookup": self.recipient, "no_wake": self.reason.value}


@dataclass(frozen=True, slots=True)
class WakeDecision(RecipientDecision):
    audience: MessageAudience
    wake_mode: WakePolicy

    def wake_policy(self) -> WakePolicy:
        return self.wake_mode

    def claim_decisions(self) -> tuple[WakeDecision, ...]:
        return (self,)

    def to_wire(self) -> dict[str, str]:
        return {"recipient_lookup": self.recipient, "audience": self.audience.value,
                "wake_mode": self.wake_mode.declared_name}


def _require_stored(message: Message) -> None:
    if message.seq <= 0:
        raise ValueError("Wake and response routes require a stored canonical Message.")


def _validated_context(
    message: Message, frozen_audience: FrozenAudience, control: ControlClassification
) -> tuple[NamedTarget, frozenset[str]]:
    """Check the full envelope exactly once before interpreting recipient rows."""
    _require_stored(message)
    if type(control) is not ControlClassification:
        raise TypeError("control must be an explicit ControlClassification.")
    if type(frozen_audience) is not FrozenAudience:
        raise TypeError("wake requires a FrozenAudience and FrozenRecipient.")
    if (
        message.seq != frozen_audience.wire_seq
        or message.message_id != frozen_audience.message_id
        or message.target != frozen_audience.exact_target
        or message.sender != frozen_audience.sender_name
    ):
        raise ValueError("message does not match the frozen audience identity.")
    candidate = freeze_audience(
        message,
        frozen_audience.recipients,
        frozen_audience.source_revision,
        sender_lookup=frozen_audience.sender_lookup,
        sender_name=frozen_audience.sender_name,
    )
    if candidate.wire_envelope_digest != frozen_audience.wire_envelope_digest:
        raise ValueError("message does not match the frozen audience envelope.")
    target = CommandTarget.named(message.target)
    if message.type.conversation_message:
        target.require_frozen_recipients(message, frozen_audience)
    return target, target.effective_mentions(message, frozen_audience)


def _member_decision(
    message: Message,
    recipient: FrozenRecipient,
    control: ControlClassification,
    target: NamedTarget,
    effective_mentions: frozenset[str],
) -> RecipientDecision:
    if not message.type.conversation_message:
        return NoWakeDecision(recipient.recipient_lookup)
    if message.notice or message.membership is not None or control.passive:
        mode = PassiveWake()
    else:
        mode = target.wake_policy(recipient, effective_mentions)
    return mode.recipient_decision(recipient.recipient_lookup, target.wake_audience(effective_mentions))


def resolve_wake(
    message: Message,
    *,
    recipient: FrozenRecipient,
    frozen_audience: FrozenAudience,
    control: ControlClassification,
) -> RecipientDecision | None:
    """Resolve one member; prefer resolve_wake_cohort for the full frozen N.

    ``None`` means the lookup/name pair is absent; NoWakeDecision is a frozen
    unmentioned observer, never a claim. Neither pure API proves bus commit.
    """
    if type(recipient) is not FrozenRecipient:
        raise TypeError("wake requires a FrozenAudience and FrozenRecipient.")
    target, effective_mentions = _validated_context(message, frozen_audience, control)
    if recipient not in frozen_audience.recipients:
        return None
    return _member_decision(message, recipient, control, target, effective_mentions)


def resolve_wake_cohort(
    message: Message, *, frozen_audience: FrozenAudience, control: ControlClassification
) -> tuple[RecipientDecision, ...]:
    """Resolve ordered full N with one pure full-envelope check and no live lookup.

    Stage3 must separately attest the committed row, recipient capture, and
    control/policy version at the original send boundary before persisting N.
    """
    target, effective_mentions = _validated_context(message, frozen_audience, control)
    return tuple(
        _member_decision(message, recipient, control, target, effective_mentions)
        for recipient in frozen_audience.recipients
    )
