"""Message page: declaration and persistence owners."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field, replace
from typing import ClassVar

from .messages import Message, MessageKind
from .page_traversal import OlderTraversal, PageCursor, PageTraversal
from .read_basis import (
    DisplayBasis, SelectedDisplay, NoDisplaySelection, MessageDisplayScope,
)
from .bus import Bus

@dataclass(frozen=True, slots=True)
class MessagePage:
    """A bounded, ascending page from the durable message log.

    Cursors are exclusive message sequence numbers. Page size counts messages.
    """

    messages: tuple[Message, ...]
    has_older: bool
    has_newer: bool
    selection: SelectedDisplay
    source_pages: tuple[MessagePage, ...] = ()

    def select(self, sequences) -> MessagePage:
        """Keep only painted messages and the corresponding original read proof."""
        return self.selection.select(self, sequences)

    @classmethod
    def combine(cls, pages: tuple[MessagePage, ...]) -> MessagePage:
        latest = next((page for page in reversed(pages) if page.messages), pages[-1])
        return replace(
            latest, messages=tuple(message for page in pages for message in page.messages),
            has_older=pages[0].has_older, has_newer=pages[-1].has_newer,
            source_pages=pages,
        )

    def evidence_for(self, message: Message) -> MessagePage | None:
        page = next(page for page in (self.source_pages or (self,))
                    if any(row.view_key == message.view_key for row in page.messages))
        return page if page.selection.has_evidence else None

    @property
    def newest_evidence(self) -> MessagePage | None:
        return self.evidence_for(self.messages[-1]) if self.messages else None

    @property
    def oldest_cursor(self) -> PageCursor | None:
        return self.messages[0].view_cursor if self.messages else None

    @property
    def newest_cursor(self) -> PageCursor | None:
        return self.messages[-1].view_cursor if self.messages else None

    def __post_init__(self) -> None:
        sequences = [message.view_order for message in self.messages]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("Message pages must contain unique messages in source order.")

    @property
    def oldest_seq(self) -> int | None:
        return self.messages[0].seq if self.messages else None

    @property
    def newest_seq(self) -> int | None:
        return self.messages[-1].seq if self.messages else None


@dataclass(slots=True)
class PageWindow:
    """One message-count bound, independent of the storage representation."""

    bounds: PageBounds
    rows: deque[Message] = field(default_factory=deque)

    def page(self, has_older: bool, has_newer: bool) -> MessagePage:
        return MessagePage(tuple(self.rows), has_older, has_newer, NoDisplaySelection())

    def take(self, traversal: PageTraversal, row: Message) -> bool:
        if not self.bounds.fits(len(self.rows) + 1):
            return False
        traversal.append(self.rows, row)
        return True


@dataclass(frozen=True, slots=True)
class PageBounds:
    """A requested page's direction and budget; the default is the newest page."""

    traversal: PageTraversal = OlderTraversal()
    limit: int = 100

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("History page limit must be positive.")

    def fits(self, count: int) -> bool:
        return count <= self.limit

    def after(self, page: MessagePage) -> PageBounds | None:
        traversal = self.traversal.continuation(page)
        limit = self.limit - len(page.messages)
        return (replace(self, traversal=traversal, limit=limit)
                if traversal is not None and limit > 0 else None)

    def conversation(self, scope: MessageDisplayScope) -> ConversationMessagePageRequest:
        return ConversationMessagePageRequest(scope, self)


NEWEST_PAGE = PageBounds()


@dataclass(frozen=True, slots=True)
class MessageReadRequest[T](ABC):
    """One opened-source/index lifetime for scoped pages and existence queries."""

    scope: MessageDisplayScope

    @property
    @abstractmethod
    def empty(self) -> T: ...

    @abstractmethod
    def read_opened(self, source: Bus) -> T: ...


@dataclass(frozen=True, slots=True)
class MessagePageRequest(MessageReadRequest[MessagePage]):
    """Captured membership, direction and bounds for indexed paging."""

    bounds: PageBounds = NEWEST_PAGE
    empty: ClassVar[MessagePage] = MessagePage((), False, False, NoDisplaySelection())

    def kinds(self):
        return MessageKind.members_with(MessageKind)

    def read_opened(self, source: Bus) -> MessagePage:
        traversal = self.bounds.traversal
        window = PageWindow(self.bounds)
        opposite = bool(traversal.opposite(source, self.scope, limit=1, kinds=self.kinds()))
        more = False
        for row in traversal.records(source, self.scope, limit=self.bounds.limit + 1, kinds=self.kinds()):
            if not window.take(traversal, row):
                more = True
                break
        return traversal.result(window, more, opposite)

    def displayed(self, reads, document, viewer, snapshot, conversation_snapshot, source):
        page = self.read_opened(source)
        return DisplayedMessagePage(page, reads.capture(
            viewer, page.messages, snapshot, source,
            conversation_snapshot=conversation_snapshot, document=document))


class ConversationMessagePageRequest(MessagePageRequest):
    """A presented conversation excludes declaration-owned state records."""

    def kinds(self):
        return tuple(kind for kind in super().kinds() if kind.conversation_message)


@dataclass(frozen=True, slots=True)
class DisplayedMessagePage:
    page: MessagePage
    displayed: DisplayBasis


@dataclass(frozen=True, slots=True)
class InboxPage(DisplayedMessagePage):
    """One unread page and the unread messages beyond it in the same source cut."""

    remaining: int
