"""Pure shadow decisions for a message and its externally frozen audience.

This module does not choose channel members, accept work, start a turn, or publish
responses. The audience owner captures both canonical names and stable lookups;
a later bus-owned boundary must attest its durable commit before live use.
"""

from __future__ import annotations

from .audience_manifest import AudiencedMessage, FrozenRecipient
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
    message: AudiencedMessage, control: type[MessageControl]
) -> tuple[NamedTarget, frozenset[str]]:
    _require_stored(message)
    target = CommandTarget.named(message.target)
    if message.type.conversation_message:
        target.require_frozen_recipients(message)
    return target, target.effective_mentions(message)


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
    audience = target.wake_audience(effective_mentions)
    return mode.recipient_decision(recipient.recipient_lookup, audience)


def resolve_wake(
    message: AudiencedMessage,
    *,
    recipient: FrozenRecipient,
    control: type[MessageControl],
) -> RecipientDecision | None:
    """Resolve one member; prefer resolve_wake_cohort for the full frozen N.

    ``None`` means the lookup/name pair is absent; NoWakeDecision is a frozen
    unmentioned observer, never a claim. Neither pure API proves bus commit.
    """
    target, effective_mentions = _validated_context(message, control)
    if recipient not in message.recipients:
        return None
    return _member_decision(message, recipient, control, target, effective_mentions)


def resolve_wake_cohort(
    message: AudiencedMessage, *, control: type[MessageControl]
) -> tuple[RecipientDecision, ...]:
    """Resolve ordered full N with one pure full-envelope check and no live lookup.

    Stage3 must separately attest the committed row, recipient capture, and
    control/policy version at the original send boundary before persisting N.
    """
    target, effective_mentions = _validated_context(message, control)
    return tuple(
        _member_decision(message, recipient, control, target, effective_mentions)
        for recipient in message.recipients
    )
