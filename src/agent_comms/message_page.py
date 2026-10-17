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
    MessageDisplayScope, NoDisplaySelection,
)
from .wire_log import OpenedWireSnapshot

if TYPE_CHECKING:
    from .historical_views import HistoricalDisplay, HistoryCursor
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


class PageTraversal(ABC):
    """Exclusive cursor, traversal order and edge meaning belong together."""

    cursor: int | None

    @staticmethod
    def capture(before: int | None, after: int | None) -> PageTraversal:
        if before is not None and after is not None:
            raise ValueError("History pages accept either before or after, not both.")
        return NewerTraversal(after) if after is not None else OlderTraversal(before)

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
class MessageReadRequest[T](ABC):
    """One opened-source/index lifetime for scoped pages and existence queries."""

    scope: MessageDisplayScope

    def includes(self, message: Message) -> bool:
        return self.scope.includes(message)

    @property
    @abstractmethod
    def empty(self) -> T: ...

    @abstractmethod
    def indexed(self, index, source: OpenedWireSnapshot) -> T: ...

    def read(self, log) -> T:
        with log._opened_wire_snapshot(need_sequence=False) as source:
            return self.read_opened(source)

    def read_opened(self, source: OpenedWireSnapshot) -> T:
        return source.query_page_index(lambda index: self.indexed(index, source), empty=self.empty)

    def indexed_records(self, index, source, rows):
        for message, size in source.indexed_records(index, rows, before_offset=source.boundary):
            if self.includes(message):
                yield MessagePageRow(message, size)


@dataclass(frozen=True, slots=True)
class MessagePageRequest(MessageReadRequest[MessagePage]):
    """Captured membership, direction and bounds for indexed paging."""

    traversal: PageTraversal
    limit: int = 100
    max_bytes: int = 256 * 1024
    empty: ClassVar[MessagePage] = MessagePage((), False, False)

    def __post_init__(self):
        if self.limit <= 0:
            raise ValueError("History page limit must be positive.")
        if self.max_bytes <= 0:
            raise ValueError("History page byte budget must be positive.")

    @classmethod
    def capture(cls, scope, *, before=None, after=None, limit=100, max_bytes=256 * 1024):
        return cls(scope, PageTraversal.capture(before, after), limit, max_bytes)

    @classmethod
    def every(cls, scope: MessageDisplayScope, *, after: int = 0) -> MessagePageRequest:
        """All of a scope's messages after a sequence, with no count or byte bound.

        The page index finds them by target, so the read costs their rows and
        not the silent records between them.
        """
        return cls(scope, NewerTraversal(after), sys.maxsize, sys.maxsize)

    def indexed(self, index, source: OpenedWireSnapshot) -> MessagePage:
        window = PageWindow(self.limit, self.max_bytes)
        opposite = more = False
        with closing(self.traversal.opposite_offsets(index, self.scope, source.boundary)) as rows:
            opposite = any(True for _ in self.indexed_records(index, source, rows))
        with closing(self.traversal.offsets(index, self.scope, source.boundary)) as rows:
            for row in self.indexed_records(index, source, rows):
                if not window.take(self.traversal, row):
                    more = True
                    break
        return self.traversal.result(window, more, opposite)


@dataclass(frozen=True, slots=True)
class UnreadBeforeRequest(MessageReadRequest[bool]):
    """Existence, not a one-item page that searches for a second result."""

    scope: DMDisplayScope
    before: int
    seen: frozenset[int]
    empty: ClassVar[bool] = False

    def includes(self, message: Message | BusPageRow) -> bool:
        return self.scope.unread_before(message, self.before, self.seen)

    def indexed(self, index, source: OpenedWireSnapshot) -> bool:
        with closing(OlderTraversal(self.before).offsets(index, self.scope, source.boundary)) as rows:
            candidates = (row for row in rows if self.includes(row))
            return any(True for _ in self.indexed_records(index, source, candidates))


class ConversationMessagePageRequest(MessagePageRequest):
    """A presented conversation excludes declaration-owned state records."""

    def includes(self, message: Message) -> bool:
        return message.type.conversation_message and super().includes(message)
