"""Cursor identity and exclusive traversal, independent of message persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .field_codec import NullMember, RecordMember, SingleFieldValue, SubclassUnion

if TYPE_CHECKING:
    from .bus import BusReader
    from .message_page import MessagePage, PageWindow
    from .messages import Message, MessageKind
    from .read_basis import MessageDisplayScope


class PageCursor(SubclassUnion, affix=""):
    """The cursor owns its bound, source and whether an opposite page exists."""

    def older(self) -> OlderTraversal:
        return OlderTraversal(self)

    def newer(self) -> NewerTraversal:
        return NewerTraversal(self)

    @property
    def history_source(self) -> str | None:
        return None

    @property
    def historical(self) -> bool:
        return False

    @property
    @abstractmethod
    def upper(self) -> int | None: ...

    @property
    @abstractmethod
    def lower(self) -> int: ...

    @abstractmethod
    def opposite_older(
        self, source: BusReader, scope: MessageDisplayScope, *, limit: int,
        kinds: tuple[type[MessageKind], ...],
    ) -> tuple[Message, ...]: ...


@dataclass(frozen=True)
class SequenceCursor(PageCursor, declared_name=None):
    sequence: int

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("History cursors cannot be negative.")

    @property
    def upper(self) -> int:
        return self.sequence

    @property
    def lower(self) -> int:
        return self.sequence

    def opposite_older(
        self, source: BusReader, scope: MessageDisplayScope, *, limit: int,
        kinds: tuple[type[MessageKind], ...],
    ) -> tuple[Message, ...]:
        return scope.records(source, lower=self.sequence - 1, limit=limit, kinds=kinds)


@dataclass(frozen=True)
class LiveCursor(SingleFieldValue, SequenceCursor):
    """Its sequence is its whole wire value."""


@dataclass(frozen=True)
class NewestCursor(NullMember, PageCursor):
    """No cursor given: start at the newest message, or oldest for newer pages."""

    @property
    def upper(self) -> None:
        return None

    @property
    def lower(self) -> int:
        return 0

    def opposite_older(
        self, source: BusReader, scope: MessageDisplayScope, *, limit: int,
        kinds: tuple[type[MessageKind], ...],
    ) -> tuple[Message, ...]:
        return ()


@dataclass(frozen=True)
class HistoryCursor(RecordMember, SequenceCursor):
    source: str

    @property
    def history_source(self) -> str:
        return self.source

    @property
    def historical(self) -> bool:
        return True


class PageTraversal(ABC):
    """Traversal order and edge meaning retain their original nominal cursor."""

    cursor: PageCursor
    # The bus sequence order this traversal reads rows in.
    seq_order: ClassVar[str]

    @property
    @abstractmethod
    def upper(self) -> int | None:
        """The exclusive newest sequence this page may hold; None leaves it unbounded."""

    def source_page(self, live: MessagePage, historical: Callable[[], MessagePage]) -> MessagePage:
        return live if live.messages else historical()

    def after_exhausted_history(
        self, view: Callable[[PageTraversal], MessagePage], result: MessagePage,
    ) -> MessagePage:
        return result

    @property
    def source(self) -> str | None:
        return self.cursor.history_source

    @property
    def historical(self) -> bool:
        return self.cursor.historical

    @abstractmethod
    def source_indexes(self, start: int, count: int) -> range: ...

    def for_source(self, current: bool) -> PageTraversal:
        return self if current else type(self)()

    @abstractmethod
    def records(
        self, source: BusReader, scope: MessageDisplayScope, *, limit: int,
        kinds: tuple[type[MessageKind], ...] = (),
    ) -> tuple[Message, ...]: ...

    @abstractmethod
    def opposite(
        self, source: BusReader, scope: MessageDisplayScope, *, limit: int,
        kinds: tuple[type[MessageKind], ...] = (),
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
    cursor: PageCursor = NewestCursor()
    seq_order = "DESC"

    @property
    def upper(self) -> int | None:
        """The exclusive newest sequence this page may hold."""
        return self.cursor.upper

    def source_indexes(self, start: int, count: int) -> range:
        return range(start, -1, -1)

    def records(
        self, source: BusReader, scope: MessageDisplayScope, *, limit: int,
        kinds: tuple[type[MessageKind], ...] = (),
    ) -> tuple[Message, ...]:
        return scope.records(source, upper=self.cursor.upper, descending=True, limit=limit, kinds=kinds)

    def opposite(
        self, source: BusReader, scope: MessageDisplayScope, *, limit: int,
        kinds: tuple[type[MessageKind], ...] = (),
    ) -> tuple[Message, ...]:
        return self.cursor.opposite_older(source, scope, limit=limit, kinds=kinds)

    def continuation(self, page: MessagePage) -> OlderTraversal | None:
        cursor = page.oldest_cursor
        return cursor.older() if page.has_older and cursor is not None else None

    append = staticmethod(deque.appendleft)

    def result(self, window: PageWindow, more: bool, opposite: bool) -> MessagePage:
        return window.page(more, opposite)


@dataclass(frozen=True, slots=True)
class NewerTraversal(PageTraversal):
    cursor: PageCursor = NewestCursor()
    seq_order = "ASC"

    @property
    def upper(self) -> None:
        return None

    def source_page(self, live: MessagePage, historical: Callable[[], MessagePage]) -> MessagePage:
        return live

    def after_exhausted_history(
        self, view: Callable[[PageTraversal], MessagePage], result: MessagePage,
    ) -> MessagePage:
        live = view(NewerTraversal())
        return replace(live, selection=replace(
            live.selection, history_revision=result.selection.history_revision,
        ))

    def source_indexes(self, start: int, count: int) -> range:
        return range(start, count)

    def records(
        self, source: BusReader, scope: MessageDisplayScope, *, limit: int,
        kinds: tuple[type[MessageKind], ...] = (),
    ) -> tuple[Message, ...]:
        return scope.records(source, lower=self.cursor.lower, limit=limit, kinds=kinds)

    def opposite(
        self, source: BusReader, scope: MessageDisplayScope, *, limit: int,
        kinds: tuple[type[MessageKind], ...] = (),
    ) -> tuple[Message, ...]:
        return scope.records(source, upper=self.cursor.lower + 1, descending=True,
                             limit=limit, kinds=kinds)

    def continuation(self, page: MessagePage) -> NewerTraversal | None:
        cursor = page.newest_cursor
        return cursor.newer() if page.has_newer and cursor is not None else None

    append = staticmethod(deque.append)

    def result(self, window: PageWindow, more: bool, opposite: bool) -> MessagePage:
        return window.page(opposite, more)
