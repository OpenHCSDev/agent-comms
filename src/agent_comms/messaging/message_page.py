from __future__ import annotations

from abc import (
    ABC,
    abstractmethod,
)
from collections import deque
from dataclasses import (
    dataclass,
    field,
    replace,
)
from typing import ClassVar

from agent_comms.messaging.bus import BusReader
from agent_comms.messaging.messages import Message, MessageKind
from agent_comms.messaging.page_traversal import OlderTraversal, PageCursor, PageTraversal
from agent_comms.messaging.read_basis import (
    DisplayBasis,
    ListedDeliveries,
    MessageDisplayScope,
    NoDisplaySelection,
    SelectedDisplay,
)
from agent_comms.transport.tool_result import ToolResult


@dataclass(frozen=True, slots=True)
class MessagePage(ToolResult):
    """A bounded, ascending page from the durable message log, and the reply a UI's page
    request returns, in process or through the connector.

    Cursors are exclusive message sequence numbers. Page size counts messages.
    """

    messages: tuple[Message, ...]
    has_older: bool
    has_newer: bool
    selection: SelectedDisplay
    source_pages: tuple[MessagePage, ...] = ()

    def same_display(self, earlier: MessagePage) -> bool:
        """Read-scope safety compares the original pages, never a client identity copy."""
        return self.selection.identity == earlier.selection.identity

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

    def message_evidence(self) -> tuple[tuple[Message, MessagePage | None], ...]:
        """Each message with the source page whose selection proves it, if that page has
        evidence. The source pages are indexed once, not rescanned per message."""
        holders = {row.view_key: page for page in (self.source_pages or (self,))
                   for row in page.messages}
        return tuple(
            (message, holder if (holder := holders[message.view_key]).selection.has_evidence else None)
            for message in self.messages)

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
    def read_opened(self, source: BusReader) -> T: ...


@dataclass(frozen=True, slots=True)
class MessagePageRequest(MessageReadRequest[MessagePage]):
    """Captured membership, direction and bounds for indexed paging."""

    bounds: PageBounds = NEWEST_PAGE
    empty: ClassVar[MessagePage] = MessagePage((), False, False, NoDisplaySelection())

    def kinds(self):
        return MessageKind.members_with(MessageKind)

    def read_opened(self, source: BusReader) -> MessagePage:
        traversal = self.bounds.traversal
        window = PageWindow(self.bounds)
        with source.reading() as reader:
            opposite = bool(traversal.opposite(reader, self.scope, limit=1, kinds=self.kinds()))
            more = False
            for row in traversal.records(reader, self.scope, limit=self.bounds.limit + 1, kinds=self.kinds()):
                if not window.take(traversal, row):
                    more = True
                    break
        return traversal.result(window, more, opposite)

    @classmethod
    def listed(cls, listing: ListedDeliveries, bounds: PageBounds) -> MessagePageRequest:
        """The page of what the route index listed; a listing of no sequences is the empty
        page without opening the bus, which on every empty watch wake ran two scoped scans."""
        return (cls if listing.sequences else NothingListedRequest)(listing, bounds)

    def displayed(self, reads, document, viewer, snapshot, conversation_snapshot, source):
        page = self.read_opened(source)
        return DisplayedMessagePage(page, reads.capture(
            viewer, page.messages, snapshot, source,
            conversation_snapshot=conversation_snapshot, document=document))


@dataclass(frozen=True, slots=True)
class NothingListedRequest(MessagePageRequest):
    """The route index listed no sequence, so the page is the declared empty page."""

    def read_opened(self, source: BusReader) -> MessagePage:
        return self.empty


class ConversationMessagePageRequest(MessagePageRequest):
    """A presented conversation excludes declaration-owned state records."""

    def kinds(self):
        return tuple(kind for kind in super().kinds() if kind.is_conversation_message())


@dataclass(frozen=True, slots=True)
class DisplayedMessagePage:
    page: MessagePage
    displayed: DisplayBasis


@dataclass(frozen=True, slots=True)
class InboxPage(DisplayedMessagePage):
    """One unread page and the unread messages beyond it in the same source cut."""

    remaining: int
