"""The source operations display choices consume, independent of their services."""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from typing import Self


class SentClocks(ABC):
    __slots__ = ()
    sent: Mapping[str, float]


class ActivitySource(ABC):
    __slots__ = ()

    @abstractmethod
    def opened_activity(self) -> AbstractContextManager[SentClocks]: ...


class SentHistorySource[Registry](ABC):
    __slots__ = ()
    bus: ActivitySource

    @abstractmethod
    def last_sent_timestamps(
        self, rows: Iterable[tuple[str, float]], *, registry: Registry,
    ) -> dict[str, float]: ...


class StoredThreadOrder[Order](ABC):
    __slots__ = ()
    order: Order


class ChannelOrderSource[ThreadOrder, ChannelOrder](ABC):
    __slots__ = ()

    @abstractmethod
    def set_channel_sort(self, name: str, order: ThreadOrder) -> StoredThreadOrder[ThreadOrder]: ...

    @abstractmethod
    def set_channel_order(self, order: ChannelOrder) -> ChannelOrder: ...


class SortedChannel[View, Order](ABC):
    __slots__ = ()
    name: str
    created_at: float

    @abstractmethod
    def sort_key(self, order: Order, view: View) -> tuple[int, int, float, float, str]: ...


class ChannelSortView[Order](ABC):
    __slots__ = ()
    channel: SortedChannel[Self, Order]
    last_activity: float
    last_user_input: float
