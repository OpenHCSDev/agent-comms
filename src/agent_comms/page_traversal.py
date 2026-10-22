"""Cursor identity and exclusive traversal, independent of message persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from .field_codec import FieldCodec, NullMember, RecordMember, SubclassUnion

if TYPE_CHECKING:
    from .bus import BusReader
    from .message_page import MessagePage, PageWindow
    from .messages import Message
    from .read_basis import MessageDisplayScope


class PageCursor(SubclassUnion, affix=""):
    sequence: int

    @abstractmethod
    def older(self) -> OlderTraversal: ...

    @abstractmethod
    def newer(self) -> NewerTraversal: ...


@dataclass(frozen=True)
class LiveCursor(PageCursor):
    sequence: int

    def older(self) -> OlderTraversal:
        return OlderTraversal(self.sequence)

    def newer(self) -> NewerTraversal:
        return NewerTraversal(self.sequence)

    def to_wire(self) -> int:
        return self.sequence

    @classmethod
    def from_wire(cls, data: object) -> Self:
        return cls(FieldCodec.decode(int, data))

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(int)


@dataclass(frozen=True)
class NewestCursor(NullMember, PageCursor):
    """No cursor given: a page starts at the newest message, or the oldest for newer pages."""

    def older(self) -> OlderTraversal:
        return OlderTraversal()

    def newer(self) -> NewerTraversal:
        return NewerTraversal()


@dataclass(frozen=True)
class HistoryCursor(RecordMember, PageCursor):
    source: str
    sequence: int

    def older(self) -> OlderTraversal:
        return OlderTraversal(self.sequence, self.source)

    def newer(self) -> NewerTraversal:
        return NewerTraversal(self.sequence, self.source)


class PageTraversal(ABC):
    """Exclusive cursor, traversal order and edge meaning belong together."""

    cursor: int | None
    source: str | None

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
    def records(
        self, source: BusReader, scope: MessageDisplayScope, *, limit: int, kinds=(),
    ) -> tuple[Message, ...]: ...

    @abstractmethod
    def opposite(
        self, source: BusReader, scope: MessageDisplayScope, *, limit: int, kinds=(),
    ) -> tuple[Message, ...]: ...

    @staticmethod
    @abstractmethod
    def append[T](rows: deque[T], row: T, /) -> None: ...

    @abstractmethod
    def continuation(self, page: MessagePage) -> PageTraversal | None: ...

    @abstractmethod
    def result(self, window: PageWindow, more: bool, opposite: bool) -> MessagePage: ...


@dataclass(frozen=True, slots=True)
class OlderTraversal(PageTraversal):
    cursor: int | None = None
    source: str | None = None

    def source_indexes(self, start: int, count: int) -> range:
        return range(start, -1, -1)

    def records(self, source, scope, *, limit, kinds=()):
        return scope.records(source, upper=self.cursor, descending=True, limit=limit, kinds=kinds)

    def opposite(self, source, scope, *, limit, kinds=()):
        if self.cursor is None:
            return ()
        return scope.records(source, lower=self.cursor - 1, limit=limit, kinds=kinds)

    def continuation(self, page: MessagePage) -> OlderTraversal | None:
        cursor = page.oldest_cursor
        return cursor.older() if page.has_older and cursor is not None else None

    append = staticmethod(deque.appendleft)

    def result(self, window: PageWindow, more: bool, opposite: bool) -> MessagePage:
        return window.page(more, opposite)


@dataclass(frozen=True, slots=True)
class NewerTraversal(PageTraversal):
    cursor: int = 0
    source: str | None = None

    def source_indexes(self, start: int, count: int) -> range:
        return range(start, count)

    def records(self, source, scope, *, limit, kinds=()):
        return scope.records(source, lower=self.cursor, limit=limit, kinds=kinds)

    def opposite(self, source, scope, *, limit, kinds=()):
        return OlderTraversal(self.cursor + 1).records(source, scope, limit=limit, kinds=kinds)

    def continuation(self, page: MessagePage) -> NewerTraversal | None:
        cursor = page.newest_cursor
        return cursor.newer() if page.has_newer and cursor is not None else None

    append = staticmethod(deque.append)

    def result(self, window: PageWindow, more: bool, opposite: bool) -> MessagePage:
        return window.page(opposite, more)
