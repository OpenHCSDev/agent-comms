"""Response semantics belong to policy store_files, independently of delivery."""

from __future__ import annotations

from .class_fact import ClassFact
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .projection_source import ProjectionSource

if TYPE_CHECKING:
    from .messages import Message
    from .threads import Thread


@dataclass(frozen=True)
class ResponsePolicy(DeclaredFamily, ProjectionSource, affix="Policy"):

    @ClassFact
    def writes(cls) -> tuple[type, ...]:
        from .bus_tables import AwaitingAnswerSource
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
        self, message: Message, audience: Sequence[str], *, aliases: Mapping[str, str] | None = None
    ) -> tuple[str, ...]:
        """Resolve this policy's channel recipients."""

    @abstractmethod
    def guidance(self, message: Message, *, aliases: Mapping[str, str] | None = None) -> str:
        """Instructions included in a queued input."""

    def starts_turn_for(self, message: Message, name: str, aliases: Mapping[str, str] | None = None) -> bool:
        return name in self.recipients(message, (name,), aliases=aliases)

    def context_peer(self, owner: str, sender: str, target: str) -> str | None:
        return None

    def disposition_key(self, message: Message, owner: Thread) -> str:
        return f"bus:{message.seq}:owner:{float(owner.created_at).hex()}"


class NoChannelRecipients(ProjectionSource):
    def recipients(
        self, message: Message, audience: Sequence[str], *, aliases: Mapping[str, str] | None = None
    ) -> tuple[str, ...]:
        return ()

    def validate_recipients(self, recipients: tuple[str, ...]) -> None:
        if recipients:
            raise ValueError("This eligibility cannot declare channel recipients.")


class DirectPolicy(NoChannelRecipients, ResponsePolicy):
    starts_turn = True
    separate_turn = True

    def starts_turn_for(self, message: Message, name: str, aliases: Mapping[str, str] | None = None) -> bool:
        return self.starts_turn

    def guidance(self, message: Message, *, aliases: Mapping[str, str] | None = None) -> str:
        return "direct; reply to the sender"

    def context_peer(self, owner: str, sender: str, target: str) -> str | None:
        return target if sender == owner else sender if target == owner else None

    def disposition_key(self, message: Message, owner: Thread) -> str:
        return f"bus:{message.seq}"


class CollectivePolicy(ResponsePolicy):
    starts_turn = True

    def recipients(
        self, message: Message, audience: Sequence[str], *, aliases: Mapping[str, str] | None = None
    ) -> tuple[str, ...]:
        return tuple(dict.fromkeys(audience))

    def guidance(self, message: Message, *, aliases: Mapping[str, str] | None = None) -> str:
        return "collective; channel members may respond"


class MentionedOnlyPolicy(ResponsePolicy):
    def recipients(
        self, message: Message, audience: Sequence[str], *, aliases: Mapping[str, str] | None = None
    ) -> tuple[str, ...]:
        aliases = aliases or {}
        selected = {aliases.get(mention.thread, mention.thread) for mention in message.mentions}
        return tuple(name for name in dict.fromkeys(audience) if name in selected)

    def guidance(self, message: Message, *, aliases: Mapping[str, str] | None = None) -> str:
        aliases = aliases or {}
        names = ", ".join(
            dict.fromkeys(
                f"@{aliases.get(mention.thread, mention.thread)}" for mention in message.mentions
            )
        )
        return (
            f"mentioned_only; only resolved mentioned identities may respond: {names}; "
            "unmentioned observers dismiss quietly"
        )


class InformationalPolicy(NoChannelRecipients, ResponsePolicy):
    def guidance(self, message: Message, *, aliases: Mapping[str, str] | None = None) -> str:
        return "informational; observe and dismiss without replying"


@dataclass(frozen=True, slots=True)
class ResponseEligibility(ProjectionSource):

    @ClassFact
    def writes(cls) -> tuple[type, ...]:
        from .bus_tables import AwaitingAnswerSource
        return (AwaitingAnswerSource,)

    policy: ResponsePolicy
    recipients: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.recipients) != len(set(self.recipients)):
            raise ValueError("Response eligibility recipients must be unique.")
        self.policy.validate_recipients(self.recipients)
