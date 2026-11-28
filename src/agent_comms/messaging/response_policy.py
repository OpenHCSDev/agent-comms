"""Response semantics belong to policy store_files, independently of delivery."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType

from agent_comms.execution.input_key import BusInputKey, InputKey, OwnedBusInputKey
from agent_comms.messaging.projection_source import ProducedRow, ProjectionSource
from agent_comms.messaging.response_message_source import ResponseMessageSource
from agent_comms.nominal.class_fact import ClassFact
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.registry.thread_creation import ThreadCreationSource

NO_ALIASES: Mapping[str, str] = MappingProxyType({})
"""No thread has been renamed: every mention keeps its committed name."""


@dataclass(frozen=True)
class ResponsePolicy(DeclaredFamily, ProjectionSource, affix="Policy"):

    @ClassFact
    def writes(cls) -> tuple[type[ProducedRow], ...]:
        from agent_comms.messaging.bus_tables import AwaitingAnswerSource
        return (AwaitingAnswerSource,)

    starts_turn = False
    separate_turn = False

    @property
    def starts_direct_turn(self) -> bool:
        return self.starts_turn and self.separate_turn

    @classmethod
    @cache
    def instance(cls) -> ResponsePolicy:
        return cls()

    def validate_recipients(self, recipients: tuple[str, ...]) -> None:
        """Channel policies allow the recipients their declaration selects."""

    @abstractmethod
    def recipients(
        self, message: ResponseMessageSource, audience: Sequence[str], *, aliases: Mapping[str, str] = NO_ALIASES
    ) -> tuple[str, ...]:
        """Resolve the channel members who may contribute a response."""

    def required_responders(
        self, message: ResponseMessageSource, audience: Sequence[str], *, aliases: Mapping[str, str] = NO_ALIASES
    ) -> tuple[str, ...]:
        """Resolve the channel members who owe a reply; a contributor need not owe one."""
        return self.recipients(message, audience, aliases=aliases)

    @abstractmethod
    def guidance(self, message: ResponseMessageSource, *, aliases: Mapping[str, str] = NO_ALIASES) -> str:
        """Instructions included in a queued input."""

    def starts_turn_for(self, message: ResponseMessageSource, name: str, aliases: Mapping[str, str] = NO_ALIASES) -> bool:
        return name in self.recipients(message, (name,), aliases=aliases)

    def direct_wake_for(self, message: ResponseMessageSource, name: str, aliases: Mapping[str, str] = NO_ALIASES) -> bool:
        """Whether a direct-inbox backend without a hidden triage phase wakes on this message."""
        return name in self.required_responders(message, (name,), aliases=aliases)

    def claims_live_reply_target_for(
        self, message: ResponseMessageSource, name: str, aliases: Mapping[str, str] = NO_ALIASES
    ) -> bool:
        """Whether steering this message into a running turn adds its route to auto-published
        reply targets.

        Unmentioned collective channel posts still steer a busy turn so the recipient reads
        them immediately, but they never hijack an active goal turn's plain-text output into
        the channel; the recipient replies explicitly via ``comms_send`` if answering.
        """
        del message, name, aliases
        return False

    def steered_guidance(
        self, message: ResponseMessageSource, *, aliases: Mapping[str, str] = NO_ALIASES
    ) -> str:
        """Instructions included when this message steers an already-running turn."""
        return self.guidance(message, aliases=aliases)

    def context_peer(self, owner: str, sender: str, target: str) -> str | None:
        return None

    def disposition_key(self, message: ResponseMessageSource, owner: ThreadCreationSource) -> InputKey:
        return OwnedBusInputKey(message.seq, float(owner.created_at))


class NoChannelRecipients(ProjectionSource):
    def recipients(
        self, message: ResponseMessageSource, audience: Sequence[str], *, aliases: Mapping[str, str] = NO_ALIASES
    ) -> tuple[str, ...]:
        return ()

    def validate_recipients(self, recipients: tuple[str, ...]) -> None:
        if recipients:
            raise ValueError("This eligibility cannot declare channel recipients.")


class DirectPolicy(NoChannelRecipients, ResponsePolicy):
    starts_turn = True
    separate_turn = True

    def starts_turn_for(self, message: ResponseMessageSource, name: str, aliases: Mapping[str, str] = NO_ALIASES) -> bool:
        return self.starts_turn

    def direct_wake_for(self, message: ResponseMessageSource, name: str, aliases: Mapping[str, str] = NO_ALIASES) -> bool:
        return self.starts_turn

    def claims_live_reply_target_for(
        self, message: ResponseMessageSource, name: str, aliases: Mapping[str, str] = NO_ALIASES
    ) -> bool:
        del message, name, aliases
        return self.starts_turn

    def guidance(self, message: ResponseMessageSource, *, aliases: Mapping[str, str] = NO_ALIASES) -> str:
        return "direct; reply to the sender"

    def context_peer(self, owner: str, sender: str, target: str) -> str | None:
        return target if sender == owner else sender if target == owner else None

    def disposition_key(self, message: ResponseMessageSource, owner: ThreadCreationSource) -> InputKey:
        return BusInputKey(message.seq)


class CollectivePolicy(ResponsePolicy):
    starts_turn = True

    def recipients(
        self, message: ResponseMessageSource, audience: Sequence[str], *, aliases: Mapping[str, str] = NO_ALIASES
    ) -> tuple[str, ...]:
        return tuple(dict.fromkeys(audience))

    def guidance(self, message: ResponseMessageSource, *, aliases: Mapping[str, str] = NO_ALIASES) -> str:
        return "collective; reply if you are answering, or say IGNORE if you are not answering (you can mark answering/not_answering with comms_ack)"

    def steered_guidance(
        self, message: ResponseMessageSource, *, aliases: Mapping[str, str] = NO_ALIASES
    ) -> str:
        del message, aliases
        return (
            "collective (steered into your active turn — plain turn text is NOT auto-sent to "
            "this channel; use comms_send with answers=[...] if you are answering, or continue "
            "your current work / call comms_ack to settle as Not answering)"
        )


class MentionedPolicy(CollectivePolicy):
    def claims_live_reply_target_for(
        self, message: ResponseMessageSource, name: str, aliases: Mapping[str, str] = NO_ALIASES
    ) -> bool:
        return self.direct_wake_for(message, name, aliases)

    def steered_guidance(
        self, message: ResponseMessageSource, *, aliases: Mapping[str, str] = NO_ALIASES
    ) -> str:
        return self.guidance(message, aliases=aliases)

    def required_responders(
        self, message: ResponseMessageSource, audience: Sequence[str], *, aliases: Mapping[str, str] = NO_ALIASES
    ) -> tuple[str, ...]:
        """Only the addressed members owe a reply; the rest of the channel may contribute."""
        mentioned = self.mentioned(message, aliases)
        return tuple(name for name in dict.fromkeys(audience) if name in mentioned)

    @staticmethod
    def mentioned(message: ResponseMessageSource, aliases: Mapping[str, str]) -> tuple[str, ...]:
        """The committed canonical mentions, under their current names."""
        return tuple(dict.fromkeys(
            aliases.get(mention.thread, mention.thread) for mention in message.mentions
        ))

    def guidance(self, message: ResponseMessageSource, *, aliases: Mapping[str, str] = NO_ALIASES) -> str:
        names = ", ".join(f"@{name}" for name in self.mentioned(message, aliases))
        return (
            f"mentioned; addressed to {names}; "
            "reply if you are answering, or say IGNORE if you are not answering (you can mark answering/not_answering with comms_ack)"
        )


class InformationalPolicy(NoChannelRecipients, ResponsePolicy):
    def guidance(self, message: ResponseMessageSource, *, aliases: Mapping[str, str] = NO_ALIASES) -> str:
        return "informational; observe and dismiss without replying"


@dataclass(frozen=True, slots=True)
class ResponseEligibility(ProjectionSource):

    @ClassFact
    def writes(cls) -> tuple[type[ProducedRow], ...]:
        from agent_comms.messaging.bus_tables import AwaitingAnswerSource
        return (AwaitingAnswerSource,)

    policy: ResponsePolicy
    recipients: tuple[str, ...] = ()
    required_responders: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.recipients) != len(set(self.recipients)):
            raise ValueError("Response eligibility recipients must be unique.")
        if not set(self.required_responders) <= set(self.recipients):
            raise ValueError("A required responder must be an eligible recipient.")
        self.policy.validate_recipients(self.recipients)
