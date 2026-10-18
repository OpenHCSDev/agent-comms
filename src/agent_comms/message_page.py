"""Message page: declaration and persistence owners."""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from collections import deque
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from .bus_page_index import BusPageIndex
from .messages import Message
from .private_path import FileRevision
from .read_basis import (
    ChannelDisplayScope, DisplaySelection, DMDisplayBasis, DMDisplayScope,
    MessageDisplayScope, NoDisplaySelection, ReadCursor,
)
from .wire_log import CurrentSnapshotOpening, WireSnapshot

if TYPE_CHECKING:
    from .read_basis import HistoricalDisplay
    from .bus_page_index import BusPageRow


@dataclass(frozen=True, slots=True)
class MessagePage:
    """A bounded, ascending page from the durable message log.

    Cursors are message sequence numbers and are exclusive when passed back as
    ``before`` or ``after``. A single oversized message is returned by itself
    so every cursor can make progress despite the byte budget.
    """

    messages: tuple[Message, ...]
    has_older: bool
    has_newer: bool
    display_scope: ChannelDisplayScope | None = None
    display_basis: DMDisplayBasis | None = None
    historical_display: HistoricalDisplay | None = None
    history_revision: FileRevision | None = None

    @property
    def selection(self) -> DisplaySelection:
        """Derive the selected conversation from this page's original evidence."""
        if self.historical_display is not None:
            return self.historical_display.selection(self.history_revision)
        if self.display_basis is not None:
            return self.display_basis.selection(self.history_revision)
        if self.display_scope is not None:
            return self.display_scope.selection(self.history_revision)
        return NoDisplaySelection()

    @property
    def oldest_cursor(self) -> int | HistoryCursor | None:
        return self.messages[0].view_cursor if self.messages else None

    @property
    def newest_cursor(self) -> int | HistoryCursor | None:
        return self.messages[-1].view_cursor if self.messages else None

    def __post_init__(self) -> None:
        sequences = [message.seq for message in self.messages]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("Message pages must contain unique messages in seq order.")

    @property
    def oldest_seq(self) -> int | None:
        return self.messages[0].seq if self.messages else None

    @property
    def newest_seq(self) -> int | None:
        return self.messages[-1].seq if self.messages else None


@dataclass(frozen=True, slots=True)
class MessagePageRow:
    message: Message
    size: int


@dataclass(frozen=True, slots=True)
class HistoryCursor:
    source: str
    sequence: int


class PageTraversal(ABC):
    """Exclusive cursor, traversal order and edge meaning belong together.

    A cursor taken from an attached history source names that source; the
    live log's cursors name none.
    """

    cursor: int | None
    source: str | None

    @staticmethod
    def capture(
        before: int | HistoryCursor | None = None, after: int | HistoryCursor | None = None
    ) -> PageTraversal:
        if before is not None and after is not None:
            raise ValueError("History pages accept either before or after, not both.")
        if isinstance(after, HistoryCursor):
            return NewerTraversal(after.sequence, after.source)
        if after is not None:
            return NewerTraversal(after)
        if isinstance(before, HistoryCursor):
            return OlderTraversal(before.sequence, before.source)
        return OlderTraversal(before)

    @property
    def historical(self) -> bool:
        return self.source is not None

    def __post_init__(self) -> None:
        if self.cursor is not None and self.cursor < 0:
            raise ValueError("History cursors cannot be negative.")

    @abstractmethod
    def source_indexes(self, start: int, count: int) -> range: ...

    def for_source(self, current: bool) -> PageTraversal:
        return self if current else type(self)()

    @abstractmethod
    def offsets(self, index: BusPageIndex, scope, boundary): ...

    @abstractmethod
    def opposite_offsets(self, index: BusPageIndex, scope, boundary): ...

    @staticmethod
    @abstractmethod
    def append(rows: deque[MessagePageRow], row: MessagePageRow) -> None: ...

    @abstractmethod
    def result(self, window: PageWindow, more: bool, opposite: bool) -> MessagePage: ...


@dataclass(frozen=True, slots=True)
class OlderTraversal(PageTraversal):
    cursor: int | None = None
    source: str | None = None

    def source_indexes(self, start, count):
        return range(start, -1, -1)

    def offsets(self, index, scope, boundary):
        return scope.offsets(index, lower=None, upper=self.cursor, descending=True,
                             before_offset=boundary)

    def opposite_offsets(self, index, scope, boundary):
        if self.cursor is not None:
            yield from scope.offsets(index, lower=self.cursor - 1, upper=None,
                                     descending=True, before_offset=boundary)

    append = staticmethod(deque.appendleft)

    def result(self, window, more, opposite):
        return MessagePage(window.messages, more, opposite)


@dataclass(frozen=True, slots=True)
class NewerTraversal(PageTraversal):
    cursor: int = 0
    source: str | None = None

    def source_indexes(self, start, count):
        return range(start, count)

    def offsets(self, index, scope, boundary):
        return scope.offsets(index, lower=self.cursor, upper=None, descending=False,
                             before_offset=boundary)

    def opposite_offsets(self, index, scope, boundary):
        return OlderTraversal(self.cursor + 1).offsets(index, scope, boundary)

    append = staticmethod(deque.append)

    def result(self, window, more, opposite):
        return MessagePage(window.messages, opposite, more)


@dataclass(slots=True)
class PageWindow:
    """One budget authority; a single oversized row always makes progress."""

    limit: int
    max_bytes: int
    rows: deque[MessagePageRow] = field(default_factory=deque)
    bytes: int = 0

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(row.message for row in self.rows)

    def take(self, traversal: PageTraversal, row: MessagePageRow) -> bool:
        if len(self.rows) >= self.limit or (self.rows and self.bytes + row.size > self.max_bytes):
            return False
        traversal.append(self.rows, row)
        self.bytes += row.size
        return True


@dataclass(frozen=True, slots=True)
class PageBounds:
    """A requested page's direction and budget; the default is the newest page."""

    traversal: PageTraversal = OlderTraversal()
    limit: int = 100
    max_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("History page limit must be positive.")
        if self.max_bytes <= 0:
            raise ValueError("History page byte budget must be positive.")

    def conversation(self, scope: MessageDisplayScope) -> ConversationMessagePageRequest:
        return ConversationMessagePageRequest(scope, self)


NEWEST_PAGE = PageBounds()


@dataclass(frozen=True, slots=True)
class MessageReadRequest[T](ABC):
    """One opened-source/index lifetime for scoped pages and existence queries."""

    scope: MessageDisplayScope

    def includes(self, message: Message) -> bool:
        return self.scope.includes(message)

    @property
    @abstractmethod
    def empty(self) -> T: ...

    @abstractmethod
    def indexed(self, index, source: WireSnapshot) -> T: ...

    def read(self, log) -> T:
        with log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as source:
            return self.read_opened(source)

    def read_opened(self, source: WireSnapshot) -> T:
        return source.query_page_index(lambda index: self.indexed(index, source))

    def indexed_records(self, source, rows):
        for message, size in source.indexed_records(rows, before_offset=source.boundary):
            if self.includes(message):
                yield MessagePageRow(message, size)


@dataclass(frozen=True, slots=True)
class MessagePageRequest(MessageReadRequest[MessagePage]):
    """Captured membership, direction and bounds for indexed paging."""

    bounds: PageBounds = NEWEST_PAGE
    empty: ClassVar[MessagePage] = MessagePage((), False, False)

    @classmethod
    def every(cls, scope: MessageDisplayScope, *, after: int = 0) -> MessagePageRequest:
        """All of a scope's messages after a sequence, with no count or byte bound.

        The page index finds them by target, so the read costs their rows and
        not the silent records between them.
        """
        return cls(scope, PageBounds(NewerTraversal(after), sys.maxsize, sys.maxsize))

    def indexed(self, index, source: WireSnapshot) -> MessagePage:
        traversal = self.bounds.traversal
        window = PageWindow(self.bounds.limit, self.bounds.max_bytes)
        opposite = more = False
        with closing(traversal.opposite_offsets(index, self.scope, source.boundary)) as rows:
            opposite = any(True for _ in self.indexed_records(source, rows))
        with closing(traversal.offsets(index, self.scope, source.boundary)) as rows:
            for row in self.indexed_records(source, rows):
                if not window.take(traversal, row):
                    more = True
                    break
        return traversal.result(window, more, opposite)


@dataclass(frozen=True, slots=True)
class UnreadBeforeRequest(MessageReadRequest[bool]):
    """Existence, not a one-item page that searches for a second result."""

    scope: DMDisplayScope
    before: int
    seen: ReadCursor
    empty: ClassVar[bool] = False

    def includes(self, message: Message | BusPageRow) -> bool:
        return self.scope.unread_before(message, self.before, self.seen)

    def indexed(self, index, source: WireSnapshot) -> bool:
        with closing(self.scope.offsets(index, lower=self.seen.high_water, upper=self.before, descending=True, before_offset=source.boundary)) as rows:
            candidates = (row for row in rows if self.includes(row))
            return any(True for _ in self.indexed_records(source, candidates))


class ConversationMessagePageRequest(MessagePageRequest):
    """A presented conversation excludes declaration-owned state records."""

    def includes(self, message: Message) -> bool:
        return message.type.conversation_message and super().includes(message)
