"""Exact counts and record-capped lower bounds, as supplied by their backend owners."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from agent_comms.nominal.field_codec import TaggedRecordMember, TaggedUnion

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

    mark: ClassVar[str] = "●"

    @property
    def badge(self) -> str:
        """What rows and tabs show: that there is something unread, never how many.

        Tristan, 2026-11-08: "number of unread messages still show instead of just a
        symbol for read/unread or styling". The count stays in ``detail``, for a reader
        who stopped to hover, and off a row that has to fit a 40-column phone.
        """
        return self.mark if self.highlighted else ""

    @property
    def highlighted(self) -> bool:
        """Whether a known unread count requires emphasis, independent of the badge."""
        return False

    @property
    def detail(self) -> str:
        """The tooltip's full words, which is the one place a number belongs."""
        return ""


@dataclass(frozen=True)
class ExactUnread(TaggedRecordMember, UnreadPresentation):
    count: int = 0

    @property
    def highlighted(self) -> bool:
        return bool(self.count)

    @property
    def detail(self) -> str:
        return f"{self.count} unread" if self.count else ""


@dataclass(frozen=True)
class ReplyUnread(TaggedRecordMember, UnreadPresentation):
    count: int

    @property
    def highlighted(self) -> bool:
        return bool(self.count)

    @property
    def detail(self) -> str:
        return f"{self.count} unread replies"


@dataclass(frozen=True)
class CappedReplyUnread(ReplyUnread):
    @classmethod
    def for_count(cls, count: int) -> CappedReplyUnread:
        return cls(count) if count else EmptyCappedReplyUnread()

    @property
    def detail(self) -> str:
        return f"at least {self.count} unread replies"


@dataclass(frozen=True)
class EmptyCappedReplyUnread(CappedReplyUnread):
    count: ClassVar[int] = 0
    detail = ""


@dataclass(frozen=True)
class IndexingUnread(TaggedRecordMember, UnreadPresentation):
    """Indexing is still running, so the row marks that it has not finished looking."""

    waiting = True
    badge = "…"
    detail = "Unread replies are still being indexed; the exact count is not yet known."


@dataclass(frozen=True)
class UnobservedUnread(TaggedRecordMember, UnreadPresentation):
    """No observation for this view's source has arrived yet."""

    detail = "Waiting for this source's unread state."
