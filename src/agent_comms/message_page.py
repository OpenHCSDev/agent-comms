"""Message page: declaration and persistence owners."""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .bus_page_index import BusPageIndex
from .messages import Message
from .private_path import FileRevision
from .read_basis import ChannelDisplayScope, DMDisplayBasis, DMDisplayScope, MessageDisplayScope
from .wire_log import OpenedWireSnapshot

if TYPE_CHECKING:
    from .historical_views import HistoricalDisplay, HistoryCursor


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

    @abstractmethod
    def for_source(self, current: bool) -> PageTraversal: ...

    @abstractmethod
    def offsets(self, index: BusPageIndex, scope, boundary): ...

    @abstractmethod
    def opposite_offsets(self, index: BusPageIndex, scope, boundary): ...

    @abstractmethod
    def append(self, window: PageWindow, message: Message, size: int) -> None: ...

    @abstractmethod
    def result(self, window: PageWindow, more: bool, opposite: bool) -> MessagePage: ...

    @abstractmethod
    def collect(self, request: MessagePageRequest, records) -> MessagePage: ...


@dataclass(frozen=True, slots=True)
class OlderTraversal(PageTraversal):
    cursor: int | None = None

    def source_indexes(self, start, count):
        return range(start, -1, -1)

    def for_source(self, current):
        return self if current else OlderTraversal()

    def offsets(self, index, scope, boundary):
        return scope.offsets(index, lower=None, upper=self.cursor, descending=True,
                             before_offset=boundary)

    def opposite_offsets(self, index, scope, boundary):
        if self.cursor is None:
            return scope.offsets(index, lower=0, upper=0, descending=True,
                                 before_offset=boundary)
        return scope.offsets(
            index, lower=self.cursor - 1, upper=None, descending=True,
            before_offset=boundary,
        )

    def append(self, window, message, size):
        window.rows.appendleft((message, size))
        window.bytes += size

    def result(self, window, more, opposite):
        return MessagePage(window.messages, more, opposite)

    def collect(self, request, records):
        window = PageWindow(request.limit, request.max_bytes)
        more = opposite = False
        for message, size in records:
            if not request.includes(message):
                continue
            if self.cursor is not None and message.seq >= self.cursor:
                opposite = True
                continue
            window.rows.append((message, size))
            window.bytes += size
            while window.exceeded:
                _, removed_size = window.rows.popleft()
                window.bytes -= removed_size
                more = True
        return self.result(window, more, opposite)


@dataclass(frozen=True, slots=True)
class NewerTraversal(PageTraversal):
    cursor: int

    def source_indexes(self, start, count):
        return range(start, count)

    def for_source(self, current):
        return self if current else NewerTraversal(0)

    def offsets(self, index, scope, boundary):
        return scope.offsets(index, lower=self.cursor, upper=None, descending=False,
                             before_offset=boundary)

    def opposite_offsets(self, index, scope, boundary):
        return scope.offsets(index, lower=None, upper=self.cursor + 1, descending=True,
                             before_offset=boundary)

    def append(self, window, message, size):
        window.rows.append((message, size))
        window.bytes += size

    def result(self, window, more, opposite):
        return MessagePage(window.messages, opposite, more)

    def collect(self, request, records):
        window = PageWindow(request.limit, request.max_bytes)
        opposite = False
        for message, size in records:
            if not request.includes(message):
                continue
            if message.seq <= self.cursor:
                opposite = True
                continue
            if window.full_with(size):
                # Leave this row for the next page; never skip to a smaller row.
                return self.result(window, True, opposite)
            self.append(window, message, size)
        return self.result(window, False, opposite)


@dataclass(slots=True)
class PageWindow:
    """One budget authority; a single oversized row always makes progress."""

    limit: int
    max_bytes: int
    rows: deque[tuple[Message, int]] = field(default_factory=deque)
    bytes: int = 0

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(message for message, _ in self.rows)

    def full_with(self, size: int) -> bool:
        return len(self.rows) >= self.limit or bool(
            self.rows and self.bytes + size > self.max_bytes
        )

    @property
    def exceeded(self) -> bool:
        return len(self.rows) > self.limit or len(self.rows) > 1 and self.bytes > self.max_bytes


@dataclass(frozen=True, slots=True)
class MessageReadRequest[T](ABC):
    """One opened-source/index lifetime for scoped pages and existence queries."""

    scope: MessageDisplayScope

    def includes(self, message: Message) -> bool:
        return self.scope.includes(message)

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
                yield message, size


@dataclass(frozen=True, slots=True)
class MessagePageRequest(MessageReadRequest[MessagePage]):
    """Captured membership, direction and bounds for both authoritative readers."""

    traversal: PageTraversal
    limit: int = 100
    max_bytes: int = 256 * 1024

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

    def collect(self, records: Iterator[tuple[Message, int]]) -> MessagePage:
        return self.traversal.collect(self, records)

    def empty(self) -> MessagePage:
        return self.collect(iter(()))

    def indexed(self, index, source: OpenedWireSnapshot) -> MessagePage:
        window = PageWindow(self.limit, self.max_bytes)
        opposite = more = False
        with closing(self.traversal.opposite_offsets(index, self.scope, source.boundary)) as rows:
            opposite = any(True for _ in self.indexed_records(index, source, rows))
        with closing(self.traversal.offsets(index, self.scope, source.boundary)) as rows:
            for message, size in self.indexed_records(index, source, rows):
                if window.full_with(size):
                    more = True
                    break
                self.traversal.append(window, message, size)
        return self.traversal.result(window, more, opposite)


@dataclass(frozen=True, slots=True)
class UnreadBeforeRequest(MessageReadRequest[bool]):
    """Existence, not a one-item page that searches for a second result."""

    scope: DMDisplayScope
    before: int
    seen: frozenset[int]

    def includes(self, message: Message | BusPageRow) -> bool:
        return self.scope.unread_before(message, self.before, self.seen)

    def empty(self) -> bool:
        return False

    def indexed(self, index, source: OpenedWireSnapshot) -> bool:
        with closing(self.scope.offsets(
            index, lower=None, upper=self.before, descending=True, before_offset=source.boundary
        )) as rows:
            candidates = (row for row in rows if self.includes(row))
            return any(True for _ in self.indexed_records(index, source, candidates))


class ConversationMessagePageRequest(MessagePageRequest):
    """A presented conversation excludes declaration-owned state records."""

    def includes(self, message: Message) -> bool:
        return message.type.conversation_message and super().includes(message)
