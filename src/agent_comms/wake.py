"""Pure shadow decisions for a message and its externally frozen audience.

This module does not choose channel members, accept work, start a turn, or publish
responses. The audience owner captures both canonical names and stable lookups;
a later bus-owned boundary must attest its durable commit before live use.
"""

from __future__ import annotations

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
class NoWakeDecision:
    """A frozen delivered member with no recipient-specific wake or claim."""

    recipient: str  # Stable recipient_lookup, never the mutable canonical name.
    reason: NoWakeReason = NoWakeReason.UNMENTIONED_OBSERVER

    def __post_init__(self) -> None:
        if not isinstance(self.recipient, str) or not self.recipient:
            raise ValueError("no-wake recipient requires a stable lookup.")
        if type(self.reason) is not NoWakeReason:
            raise TypeError("no-wake reason must be a NoWakeReason.")

    def initial_notification(self, recipient, *, observation):
        return PassiveWake.initial_state()().notification(
            recipient, observation=observation)


@dataclass(frozen=True, slots=True)
class WakeDecision:
    recipient: str  # Stable recipient_lookup, never the mutable canonical name.
    audience: MessageAudience
    wake_mode: WakePolicy

    def __post_init__(self) -> None:
        if not isinstance(self.recipient, str) or not self.recipient:
            raise ValueError("decision recipient requires a stable lookup.")
        if type(self.audience) is not MessageAudience or not isinstance(self.wake_mode, WakePolicy):
            raise TypeError("wake decision requires MessageAudience and a nominal WakePolicy.")

    def initial_notification(self, recipient, *, observation):
        return self.wake_mode.initial_state()().notification(
            recipient, observation=observation)


def _require_stored(message: Message) -> None:
    if not isinstance(message, Message) or message.seq <= 0:
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
) -> WakeDecision | NoWakeDecision:
    if not message.type.conversation_message:
        return NoWakeDecision(recipient.recipient_lookup)
    if message.notice or message.membership is not None or control.passive:
        mode = PassiveWake()
    else:
        mode = target.wake_policy(recipient, effective_mentions)
    if mode is None:
        return NoWakeDecision(recipient.recipient_lookup)
    return WakeDecision(recipient.recipient_lookup, target.wake_audience(effective_mentions), mode)


def resolve_wake(
    message: Message,
    *,
    recipient: FrozenRecipient,
    frozen_audience: FrozenAudience,
    control: ControlClassification,
) -> WakeDecision | NoWakeDecision | None:
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
) -> tuple[WakeDecision | NoWakeDecision, ...]:
    """Resolve ordered full N with one pure full-envelope check and no live lookup.

    Stage3 must separately attest the committed row, recipient capture, and
    control/policy version at the original send boundary before persisting N.
    """
    target, effective_mentions = _validated_context(message, frozen_audience, control)
    return tuple(
        _member_decision(message, recipient, control, target, effective_mentions)
        for recipient in frozen_audience.recipients
    )
