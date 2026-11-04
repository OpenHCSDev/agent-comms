"""Exact counts and record-capped lower bounds, as supplied by their backend owners."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from agent_comms.nominal.field_codec import TaggedUnion
from agent_comms.nominal.field_codec import TaggedRecordMember

if TYPE_CHECKING:
    from agent_comms.presentation import CoordinationSnapshot


class UnreadPresentation(TaggedUnion, affix="Unread"):
    """A row's unread count as presented. It travels with the sidebar snapshot to a remote
    UI, and several members share the same field, so each record carries its kind."""

    waiting: ClassVar[bool] = False


    @classmethod
    def for_thread(cls, snapshot: CoordinationSnapshot, name: str) -> UnreadPresentation:
        return snapshot.thread_unread[name]

    @classmethod
    def for_bus(cls, snapshot: CoordinationSnapshot, name: str) -> UnreadPresentation:
        return snapshot.unread[name]

    @classmethod
    def for_channel(cls, snapshot: CoordinationSnapshot, name: str) -> UnreadPresentation:
        return snapshot.channel_unread[name]

    @property
    @abstractmethod
    def label(self) -> str:
        """The badge rows and tabs show."""

    @property
    def highlighted(self) -> bool:
        """Whether a known unread count requires emphasis, independent of the badge."""
        return False

    detail = ""


@dataclass(frozen=True)
class ExactUnread(TaggedRecordMember, UnreadPresentation):
    count: int = 0

    @property
    def label(self) -> str:
        return f"({self.count})" if self.count else ""

    @property
    def highlighted(self) -> bool:
        return bool(self.count)


@dataclass(frozen=True)
class ReplyUnread(TaggedRecordMember, UnreadPresentation):
    count: int

    @property
    def label(self) -> str:
        return f"({self.count})"

    @property
    def highlighted(self) -> bool:
        return bool(self.count)


@dataclass(frozen=True)
class CappedReplyUnread(ReplyUnread):
    @classmethod
    def for_count(cls, count: int) -> CappedReplyUnread:
        return cls(count) if count else EmptyCappedReplyUnread()

    @property
    def label(self) -> str:
        return f"({self.count}+)"


@dataclass(frozen=True)
class EmptyCappedReplyUnread(CappedReplyUnread):
    count: ClassVar[int] = 0
    label = ""


@dataclass(frozen=True)
class IndexingUnread(TaggedRecordMember, UnreadPresentation):
    waiting = True
    label = "Indexing…"
    detail = "Unread replies are still being indexed; the exact count is not yet known."


@dataclass(frozen=True)
class UnobservedUnread(TaggedRecordMember, UnreadPresentation):
    """No observation for this view's source has arrived yet."""

    label = ""
    detail = "Waiting for this source's unread state."
