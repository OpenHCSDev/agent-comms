"""Pure shadow decisions for a message and its externally frozen audience.

This module does not choose channel members, accept work, start a turn, or publish
responses. The audience owner captures both canonical names and stable lookups;
a later bus-owned boundary must attest its durable commit before live use.
"""

from __future__ import annotations

from .audience_manifest import FrozenAudience, FrozenRecipient, freeze_audience
from .channel_targets import CommandTarget, NamedTarget
from .declared_family import DeclaredFamily
from .messages import Message
from .wake_policy import NoWakeDecision, PassiveWake, RecipientDecision, WakePolicy


class MessageControl(DeclaredFamily, affix="Control"):
    """Explicit system-control classification; not inferred from text or ACK type."""

    @classmethod
    def wake_mode(
        cls, target: NamedTarget, recipient: FrozenRecipient, mentions: frozenset[str],
    ) -> WakePolicy:
        return target.wake_policy(recipient, mentions)


class OrdinaryControl(MessageControl):
    pass


class SystemControl(MessageControl, declared_name="system_control"):
    @classmethod
    def wake_mode(
        cls, target: NamedTarget, recipient: FrozenRecipient, mentions: frozenset[str],
    ) -> WakePolicy:
        return PassiveWake()


def _require_stored(message: Message) -> None:
    if message.seq <= 0:
        raise ValueError("Wake and response routes require a stored canonical Message.")


def _validated_context(
    message: Message, frozen_audience: FrozenAudience, control: type[MessageControl]
) -> tuple[NamedTarget, frozenset[str]]:
    """Check the full envelope exactly once before interpreting recipient rows."""
    _require_stored(message)
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
    control: type[MessageControl],
    target: NamedTarget,
    effective_mentions: frozenset[str],
) -> RecipientDecision:
    if not message.type.conversation_message:
        return NoWakeDecision(recipient.recipient_lookup)
    mode = message.role.wake_mode(target, recipient, effective_mentions, control)
    return mode.recipient_decision(recipient.recipient_lookup, target.wake_audience(effective_mentions))


def resolve_wake(
    message: Message,
    *,
    recipient: FrozenRecipient,
    frozen_audience: FrozenAudience,
    control: type[MessageControl],
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
    message: Message, *, frozen_audience: FrozenAudience, control: type[MessageControl]
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
