"""Message page: declaration and persistence owners."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .bus_page_index import BusPageIndex, StaleBusPageIndexError
from .messages import Message
from .read_basis import ChannelDisplayScope, DMDisplayBasis, MessageDisplayScope

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
    history_revision: tuple[int, int, int, int] | None = None

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
    def offsets(self, index: BusPageIndex, targets): ...

    @abstractmethod
    def opposite_offsets(self, index: BusPageIndex, targets): ...

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

    def offsets(self, index, targets):
        return index.offsets(lower=None, upper=self.cursor, descending=True, targets=targets)

    def opposite_offsets(self, index, targets):
        if self.cursor is None:
            return index.offsets(lower=0, upper=0, descending=True, targets=targets)
        return index.offsets(
            lower=self.cursor - 1, upper=None, descending=True, targets=targets
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
            if not request.scope.includes(message):
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

    def offsets(self, index, targets):
        return index.offsets(lower=self.cursor, upper=None, descending=False, targets=targets)

    def opposite_offsets(self, index, targets):
        return index.offsets(lower=None, upper=self.cursor + 1, descending=True, targets=targets)

    def append(self, window, message, size):
        window.rows.append((message, size))
        window.bytes += size

    def result(self, window, more, opposite):
        return MessagePage(window.messages, opposite, more)

    def collect(self, request, records):
        window = PageWindow(request.limit, request.max_bytes)
        opposite = False
        for message, size in records:
            if not request.scope.includes(message):
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
class MessagePageRequest:
    """Captured membership, direction and bounds for both authoritative readers."""

    scope: MessageDisplayScope
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

    def collect(self, records: Iterator[tuple[Message, int]]) -> MessagePage:
        return self.traversal.collect(self, records)

    def read(self, log) -> MessagePage:
        with log.locked():
            access = log.read_metadata_unlocked().access
            try:
                with access.open_page_index(log.path) as index:
                    if access.prepare_page_index(index):
                        return self.indexed(index, log)
            except (OSError, sqlite3.DatabaseError, StaleBusPageIndexError):
                # A disposable index cannot replace the durable wire authority.
                pass
        # A missing derived index does not grant a reader the writer's lock for
        # a full scan. The canonical log owns the opened inode and byte bound.
        with log._record_snapshot(need_sequence=False) as (_, records):
            return self.collect(records)

    def indexed(self, index, log) -> MessagePage:
        window = PageWindow(self.limit, self.max_bytes)
        marker = log._private_marker_unlocked()
        targets = self.scope.index_targets
        opposite = more = False
        with log.path.open("rb") as stream:
            with closing(self.traversal.opposite_offsets(index, targets)) as rows:
                for row in rows:
                    (message, _), = log._public_page_records(*index.record(stream, row), marker)
                    if self.scope.includes(message):
                        opposite = True
                        break
            with closing(self.traversal.offsets(index, targets)) as rows:
                for row in rows:
                    (message, size), = log._public_page_records(*index.record(stream, row), marker)
                    if not self.scope.includes(message):
                        continue
                    if window.full_with(size):
                        more = True
                        break
                    self.traversal.append(window, message, size)
        return self.traversal.result(window, more, opposite)
