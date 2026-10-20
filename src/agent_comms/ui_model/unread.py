"""Exact counts and record-capped lower bounds, as supplied by their backend owners."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from agent_comms.channel_targets import CommandTarget
    from agent_comms.presentation import CoordinationSnapshot


class UnreadPresentation(ABC):
    pending = False

    @staticmethod
    def for_view(snapshot: CoordinationSnapshot, target: CommandTarget) -> UnreadPresentation:
        return snapshot.unread[target]

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
class ExactUnread(UnreadPresentation):
    count: int = 0

    @property
    def label(self) -> str:
        return f"({self.count})" if self.count else ""

    @property
    def highlighted(self) -> bool:
        return bool(self.count)


@dataclass(frozen=True)
class ReplyUnread(UnreadPresentation):
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
    def from_count(cls, count: int) -> CappedReplyUnread:
        return cls(count) if count else EmptyCappedReplyUnread()

    @property
    def label(self) -> str:
        return f"({self.count}+)"


@dataclass(frozen=True)
class EmptyCappedReplyUnread(CappedReplyUnread):
    count: ClassVar[int] = 0
    label = ""


@dataclass(frozen=True)
class IndexingUnread(UnreadPresentation):
    pending = True
    label = "Indexing…"
    detail = "Unread replies are still being indexed; the exact count is not yet known."


@dataclass(frozen=True)
class UnobservedUnread(UnreadPresentation):
    """No observation for this view's source has arrived yet."""

    pending = True
    label = ""
    detail = "Waiting for this source's unread state."
