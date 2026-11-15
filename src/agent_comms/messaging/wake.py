"""Pure shadow decisions for a message and its externally frozen audience.

This module does not choose channel members, accept work, start a turn, or publish
responses. The audience owner captures both canonical names and stable lookups;
a later bus-owned boundary must attest its durable commit before live use.
"""

from __future__ import annotations

from agent_comms.messaging.audience_manifest import AudiencedMessage
from agent_comms.messaging.channel_targets import CommandTarget, NamedTarget
from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.messaging.messages import Message
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import ThreadLookup
from agent_comms.waking.wake_policy import (
    NoWakeDecision,
    NoWakeReason,
    PassiveWake,
    RecipientDecision,
    WakePolicy,
)


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
    if message.type.is_conversation_message():
        target.require_frozen_recipients(message)
    return target, target.effective_mentions(message)


def _member_decision(
    message: Message,
    recipient: FrozenRecipient,
    control: type[MessageControl],
    target: NamedTarget,
    effective_mentions: frozenset[str],
    muted: frozenset[ThreadLookup],
) -> RecipientDecision:
    if not message.type.is_conversation_message():
        return NoWakeDecision(recipient.recipient_lookup)
    if recipient.recipient_lookup in muted:
        return NoWakeDecision(recipient.recipient_lookup, NoWakeReason.MUTED)
    mode = message.role.wake_mode(target, recipient, effective_mentions, control)
    audience = target.wake_audience(effective_mentions)
    return mode.recipient_decision(recipient.recipient_lookup, audience)


def resolve_wake(
    message: AudiencedMessage,
    *,
    recipient: FrozenRecipient,
    control: type[MessageControl],
    muted: frozenset[ThreadLookup] = frozenset(),
) -> RecipientDecision | None:
    """Resolve one member; prefer resolve_wake_cohort for the full frozen N.

    ``None`` means the lookup/name pair is absent; NoWakeDecision is a frozen
    unmentioned observer, never a claim. Neither pure API proves bus commit.
    ``muted`` carries the lookups whose wake the caller has resolved to suppress;
    they still receive the message, only their wake is dropped.
    """
    target, effective_mentions = _validated_context(message, control)
    if recipient not in message.recipients:
        return None
    return _member_decision(message, recipient, control, target, effective_mentions, muted)


def resolve_wake_cohort(
    message: AudiencedMessage, *, control: type[MessageControl],
    muted: frozenset[ThreadLookup] = frozenset(),
) -> tuple[RecipientDecision, ...]:
    """Resolve ordered full N with one pure full-envelope check and no live lookup.

    Stage3 must separately attest the committed row, recipient capture, and
    control/policy version at the original send boundary before persisting N.
    ``muted`` is the caller's resolved set of suppressed-wake lookups (read from the
    mute ledger at publish): every member is still a recipient, a muted one gets a
    MUTED no-wake decision instead of a triage wake.
    """
    target, effective_mentions = _validated_context(message, control)
    return tuple(
        _member_decision(message, recipient, control, target, effective_mentions, muted)
        for recipient in message.recipients
    )
