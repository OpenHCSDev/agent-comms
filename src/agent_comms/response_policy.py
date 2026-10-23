"""Response semantics belong to policy store_files, independently of delivery."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .mentions import ThreadMention

if TYPE_CHECKING:
    from .messages import Message
    from .threads import Thread


@dataclass(frozen=True)
class ResponsePolicy(DeclaredFamily, affix="Policy"):
    starts_turn = False
    separate_turn = False

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

    @classmethod
    @abstractmethod
    def recipients_sql(cls, mentions: str, audience: Sequence[str]) -> tuple[str, tuple]:
        """Select the same channel recipients from committed message fields."""

    @abstractmethod
    def guidance(self, message: Message, *, aliases: Mapping[str, str] | None = None) -> str:
        """Instructions included in a queued input."""

    def starts_turn_for(self, message: Message, name: str, aliases: Mapping[str, str] | None = None) -> bool:
        return name in self.recipients(message, (name,), aliases=aliases)

    def context_peer(self, owner: str, sender: str, target: str) -> str | None:
        return None

    def disposition_key(self, message: Message, owner: Thread) -> str:
        return f"bus:{message.seq}:owner:{float(owner.created_at).hex()}"


class NoChannelRecipients:
    def recipients(
        self, message: Message, audience: Sequence[str], *, aliases: Mapping[str, str] | None = None
    ) -> tuple[str, ...]:
        return ()

    @classmethod
    def recipients_sql(cls, mentions: str, audience: Sequence[str]) -> tuple[str, tuple]:
        return "0", ()

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

    @classmethod
    def recipients_sql(cls, mentions: str, audience: Sequence[str]) -> tuple[str, tuple]:
        return ("1" if audience else "0"), ()

    def recipients(
        self, message: Message, audience: Sequence[str], *, aliases: Mapping[str, str] | None = None
    ) -> tuple[str, ...]:
        return tuple(dict.fromkeys(audience))

    def guidance(self, message: Message, *, aliases: Mapping[str, str] | None = None) -> str:
        return "collective; channel members may respond"


class MentionedOnlyPolicy(ResponsePolicy):
    @classmethod
    def recipients_sql(cls, mentions: str, audience: Sequence[str]) -> tuple[str, tuple]:
        key = FieldCodec.field_plan(ThreadMention, "thread").key
        marks = ",".join("?" for _ in audience)
        return (
            f"EXISTS (SELECT 1 FROM json_each({mentions}) AS mention "
            f"WHERE json_extract(mention.value, ?) IN ({marks}))",
            (f"$.{key}", *audience),
        )

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
class ResponseEligibility:
    policy: ResponsePolicy
    recipients: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.recipients) != len(set(self.recipients)):
            raise ValueError("Response eligibility recipients must be unique.")
        self.policy.validate_recipients(self.recipients)
