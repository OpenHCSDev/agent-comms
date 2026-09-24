"""Pure shadow decisions for a message and its externally frozen audience.

This module does not choose channel members, accept work, start a turn, or publish
responses. The audience owner captures both canonical names and stable lookups;
a later bus-owned boundary must attest its durable commit before live use.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .audience_manifest import FrozenAudience, FrozenRecipient, freeze_audience
from .coordination import MessageAudience, WakeMode
from .declarations import Message, is_channel_target


class ControlClassification(StrEnum):
    """Explicit system-control classification; not inferred from text or ACK type."""

    ORDINARY = "ordinary"
    SYSTEM_CONTROL = "system_control"


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


@dataclass(frozen=True, slots=True)
class WakeDecision:
    recipient: str  # Stable recipient_lookup, never the mutable canonical name.
    audience: MessageAudience
    wake_mode: WakeMode

    def __post_init__(self) -> None:
        if not isinstance(self.recipient, str) or not self.recipient:
            raise ValueError("decision recipient requires a stable lookup.")
        if type(self.audience) is not MessageAudience or type(self.wake_mode) is not WakeMode:
            raise TypeError("wake decision requires exact MessageAudience and WakeMode enums.")


def _require_stored(message: Message) -> None:
    if not isinstance(message, Message) or message.seq <= 0 or message.target == "broadcast":
        raise ValueError("Wake and response routes require a stored canonical Message.")


def _validated_context(
    message: Message, frozen_audience: FrozenAudience, control: ControlClassification
) -> tuple[bool, frozenset[str]]:
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
    channel = is_channel_target(message.target)
    if not channel:
        if len(frozen_audience.recipients) != 1:
            raise ValueError("a direct message must have exactly one frozen eligible recipient.")
        if frozen_audience.recipients[0].canonical_thread != message.target:
            raise ValueError("direct frozen recipient must match the exact stored target.")
    effective_mentions = (
        frozenset(mention.thread for mention in message.mentions).intersection(
            frozen_audience.canonical_members
        )
        if channel
        else frozenset()
    )
    return channel, effective_mentions


def _member_decision(
    message: Message,
    recipient: FrozenRecipient,
    control: ControlClassification,
    channel: bool,
    effective_mentions: frozenset[str],
) -> WakeDecision | NoWakeDecision:
    if not channel:
        audience = MessageAudience.DIRECT
    elif effective_mentions:
        audience = MessageAudience.MENTIONED
    else:
        audience = MessageAudience.COLLECTIVE
    if (
        message.notice
        or message.membership is not None
        or control is ControlClassification.SYSTEM_CONTROL
    ):
        mode = WakeMode.PASSIVE
    elif audience is MessageAudience.DIRECT:
        mode = WakeMode.FULL
    elif audience is MessageAudience.MENTIONED:
        if recipient.canonical_thread not in effective_mentions:
            return NoWakeDecision(recipient.recipient_lookup)
        mode = WakeMode.FULL
    else:
        mode = WakeMode.BOUNDED_TRIAGE
    return WakeDecision(recipient.recipient_lookup, audience, mode)


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
    channel, effective_mentions = _validated_context(message, frozen_audience, control)
    if recipient not in frozen_audience.recipients:
        return None
    return _member_decision(message, recipient, control, channel, effective_mentions)


def resolve_wake_cohort(
    message: Message, *, frozen_audience: FrozenAudience, control: ControlClassification
) -> tuple[WakeDecision | NoWakeDecision, ...]:
    """Resolve ordered full N with one pure full-envelope check and no live lookup.

    Stage3 must separately attest the committed row, recipient capture, and
    control/policy version at the original send boundary before persisting N.
    """
    channel, effective_mentions = _validated_context(message, frozen_audience, control)
    return tuple(
        _member_decision(message, recipient, control, channel, effective_mentions)
        for recipient in frozen_audience.recipients
    )


def derive_exact_reply_target(message: Message | None) -> str | None:
    """Derive a wire origin's route independently of sender role and wake mode.

    Channel responses retain the stored exact target, including ``#all``;
    direct responses go to the stored original sender. A non-wire turn has no
    wire route. This does not create, settle, or publish a response obligation.
    """
    if message is None:
        return None
    _require_stored(message)
    return message.target if is_channel_target(message.target) else message.sender
