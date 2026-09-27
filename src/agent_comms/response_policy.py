"""Response semantics belong to policy declarations, independently of delivery."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .declarations import Message, Thread


@dataclass(frozen=True)
class ResponsePolicy(DeclaredFamily, affix="Policy"):
    DIRECT: ClassVar[ResponsePolicy]
    COLLECTIVE: ClassVar[ResponsePolicy]
    MENTIONED_ONLY: ClassVar[ResponsePolicy]
    INFORMATIONAL: ClassVar[ResponsePolicy]
    starts_turn = False
    separate_turn = False

    @property
    def value(self) -> str:
        return self.declared_name

    @classmethod
    def resolve(cls, value: ResponsePolicy | str) -> ResponsePolicy:
        return cls.decode(value).instance() if isinstance(value, str) else value

    @classmethod
    @cache
    def instance(cls) -> ResponsePolicy:
        return cls()

    def validate_recipients(self, recipients: tuple[str, ...]) -> None:
        """Channel policies admit the recipients their declaration selects."""

    def recipients(
        self, message: Message, audience: Sequence[str], *, aliases: Mapping[str, str] | None = None
    ) -> tuple[str, ...]:
        return ()

    @abstractmethod
    def guidance(self, message: Message, *, aliases: Mapping[str, str] | None = None) -> str:
        """Instructions included in a queued input."""

    def disposition_key(self, message: Message, owner: Thread) -> str:
        return f"bus:{message.seq}:owner:{float(owner.created_at).hex()}"


class NoChannelRecipients:
    def validate_recipients(self, recipients: tuple[str, ...]) -> None:
        if recipients:
            raise ValueError("This eligibility cannot declare channel recipients.")


class DirectPolicy(NoChannelRecipients, ResponsePolicy):
    starts_turn = True
    separate_turn = True

    def guidance(self, message: Message, *, aliases: Mapping[str, str] | None = None) -> str:
        return "direct; reply to the sender"

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


# Compatibility names are projections of the registered declarations, not a roster.
for _policy in ResponsePolicy.members_with(ResponsePolicy):
    setattr(ResponsePolicy, _policy.declared_name.upper(), _policy.instance())
del _policy
