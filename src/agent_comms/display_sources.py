"""The source operations display choices consume, independent of their services."""

from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from typing import Protocol, Self


class SentClocks(Protocol):
    @property
    def sent(self) -> Mapping[str, float]: ...


class ActivitySource(Protocol):
    def opened_activity(self) -> AbstractContextManager[SentClocks]: ...


class SentHistorySource[Registry](Protocol):
    @property
    def bus(self) -> ActivitySource: ...

    def last_sent_timestamps(
        self, rows: Iterable[tuple[str, float]], *, registry: Registry,
    ) -> dict[str, float]: ...


class StoredThreadOrder[Order](Protocol):
    @property
    def order(self) -> Order: ...


class ChannelOrderSource[ThreadOrder, ChannelOrder](Protocol):
    def set_channel_sort(self, name: str, order: ThreadOrder) -> StoredThreadOrder[ThreadOrder]: ...

    def set_channel_order(self, order: ChannelOrder) -> ChannelOrder: ...


class SortedChannel[View, Order](Protocol):
    @property
    def name(self) -> str: ...

    @property
    def created_at(self) -> float: ...

    def sort_key(self, order: Order, view: View) -> tuple[int, int, float, float, str]: ...


class ChannelSortView[Order](Protocol):
    @property
    def channel(self) -> SortedChannel[Self, Order]: ...

    @property
    def last_activity(self) -> float: ...

    @property
    def last_user_input(self) -> float: ...
