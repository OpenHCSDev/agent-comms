"""Bus position and content identity are one reference; neither alone is proof."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from .field_codec import NullMember, SingleFieldValue, SubclassUnion


@dataclass(frozen=True, slots=True)
class MessageReference:
    seq: int
    message_id: str


class InboxStart(SubclassUnion, affix="Start"):
    """Where an unread read begins: after a message already taken, or at the first unread."""

    @abstractmethod
    def references(self) -> tuple[MessageReference, ...]:
        """The references the read must load to check its starting point."""

    @abstractmethod
    def cursor(self) -> int:
        """The exclusive bus sequence the read starts after."""


@dataclass(frozen=True, slots=True)
class AfterMessageStart(SingleFieldValue, InboxStart):
    reference: MessageReference

    def references(self) -> tuple[MessageReference, ...]:
        return (self.reference,)

    def cursor(self) -> int:
        return self.reference.seq


@dataclass(frozen=True, slots=True)
class FirstUnreadStart(NullMember, InboxStart):
    def references(self) -> tuple[MessageReference, ...]:
        return ()

    def cursor(self) -> int:
        return 0
