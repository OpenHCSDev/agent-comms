"""What a thread's or channel's unread state shows; only complete counts are numbers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_comms.presentation import CoordinationSnapshot


class UnreadPresentation(ABC):
    @classmethod
    def _for_count(
        cls, snapshot: CoordinationSnapshot, name: str, counts: Mapping[str, int]
    ) -> UnreadPresentation:
        if name in snapshot.unread_pending:
            return IndexingUnread()
        return ExactUnread(counts.get(name, 0))

    @classmethod
    def for_thread(cls, snapshot: CoordinationSnapshot, name: str) -> UnreadPresentation:
        return cls._for_count(snapshot, name, snapshot.thread_unread)

    @classmethod
    def for_bus(cls, snapshot: CoordinationSnapshot, name: str) -> UnreadPresentation:
        return cls._for_count(snapshot, name, snapshot.unread)

    @classmethod
    def for_channel(cls, snapshot: CoordinationSnapshot, name: str) -> UnreadPresentation:
        return cls._for_count(snapshot, name, snapshot.channel_unread)

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
class IndexingUnread(UnreadPresentation):
    label = "Indexing…"
    detail = "Unread replies are still being indexed; the exact count is not yet known."


@dataclass(frozen=True)
class UnobservedUnread(UnreadPresentation):
    """No observation for this view's source has arrived yet."""

    label = ""
    detail = "Waiting for this source's unread state."
