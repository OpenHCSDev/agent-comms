"""What a thread's or channel's unread state shows; only complete counts are numbers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_comms.presentation import CoordinationSnapshot


class UnreadPresentation(ABC):
    @classmethod
    def for_thread(cls, snapshot: CoordinationSnapshot, name: str) -> UnreadPresentation:
        if name in snapshot.thread_unread_pending:
            return IndexingUnread()
        return ExactUnread(snapshot.thread_unread.get(name, 0))

    @property
    @abstractmethod
    def label(self) -> str:
        """The badge rows and tabs show."""

    @property
    def highlighted(self) -> bool:
        return bool(self.label)

    @property
    def detail(self) -> str:
        return ""


@dataclass(frozen=True)
class ExactUnread(UnreadPresentation):
    count: int = 0

    @property
    def label(self) -> str:
        return f"({self.count})" if self.count else ""


@dataclass(frozen=True)
class IndexingUnread(UnreadPresentation):
    @property
    def label(self) -> str:
        return "Indexing…"

    @property
    def detail(self) -> str:
        return "Unread replies are still being indexed; the exact count is not yet known."
