"""Persisted display choices own ordering; adapters never interpret their names."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from .channel_targets import BuiltinChannel
from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .channel_management import ChannelManagement
    from .history_views import HistoryViews
    from .presentation import ChannelView


class DisplayOrder(ABC):
    title: ClassVar[str]

    @classmethod
    def label(cls) -> str:
        return cls.title

    @classmethod
    @abstractmethod
    def store_channel_order(cls, channels: ChannelManagement, channel: str | None) -> type[DisplayOrder]: ...


class ThreadSort(DisplayOrder, DeclaredFamily, affix="ThreadSort"):
    """Newest chosen clock, then creation time and case-folded name."""

    @classmethod
    def key(
        cls, name: str, created: float, activity: float, sent: float
    ) -> tuple[float, float, str]:
        return -cls.timestamp(created, activity, sent), -created, name.casefold()

    @staticmethod
    @abstractmethod
    def timestamp(created: float, activity: float, sent: float) -> float: ...

    @classmethod
    def store_channel_order(cls, channels: ChannelManagement, channel: str | None) -> type[ThreadSort]:
        if channel is None:
            raise ValueError("Member ordering requires a channel")
        return channels.set_channel_sort(channel, cls).order

    @staticmethod
    def sent_timestamps(views: HistoryViews) -> dict[str, float]:
        """Only ordering by sent messages requires this source."""
        return {}


class LastMessageThreadSort(ThreadSort, declared_name="last_message_sent"):
    title = "Last message sent"

    @staticmethod
    def timestamp(created: float, activity: float, sent: float) -> float:
        return sent

    @staticmethod
    def sent_timestamps(views: HistoryViews) -> dict[str, float]:
        return views.last_sent_timestamps()


class CreatedThreadSort(ThreadSort, declared_name="created_at"):
    title = "Date created"

    @staticmethod
    def timestamp(created: float, activity: float, sent: float) -> float:
        return created


class LastActivityThreadSort(ThreadSort):
    title = "Last activity"

    @staticmethod
    def timestamp(created: float, activity: float, sent: float) -> float:
        return activity


class ChannelSort(DisplayOrder, DeclaredFamily, affix="ChannelSort"):
    """Pins partition every order; builtins keep their declared order in each partition."""

    @classmethod
    def key(cls, view: ChannelView) -> tuple[int, int, float, float, str]:
        builtins = tuple(BuiltinChannel)
        builtin = view.channel.builtin
        if builtin is not None:
            return not view.channel.pinned, builtins.index(builtin), 0, 0, view.channel.name
        return not view.channel.pinned, len(builtins), *cls.sort_fields(view)

    @classmethod
    def store_channel_order(cls, channels: ChannelManagement, channel: str | None) -> type[ChannelSort]:
        return channels.set_channel_order(cls)

    @classmethod
    @abstractmethod
    def sort_fields(cls, view: ChannelView) -> tuple[float, float, str]: ...


class NameChannelSort(ChannelSort):
    title = "Name"

    @classmethod
    def sort_fields(cls, view: ChannelView) -> tuple[float, float, str]:
        return 0, 0, view.channel.name.casefold()


class RecentChannelSort(ChannelSort):
    """Clock-based channel orders share creation/name tie-breaks, not name ordering."""

    @classmethod
    def sort_fields(cls, view: ChannelView) -> tuple[float, float, str]:
        return -cls.timestamp(view), -view.channel.created_at, view.channel.name.casefold()

    @staticmethod
    @abstractmethod
    def timestamp(view: ChannelView) -> float: ...


class CreatedChannelSort(RecentChannelSort, declared_name="created_at"):
    title = "Date created"

    @staticmethod
    def timestamp(view: ChannelView) -> float:
        return view.channel.created_at


class LastActivityChannelSort(RecentChannelSort):
    title = "Last activity"

    @staticmethod
    def timestamp(view: ChannelView) -> float:
        return view.last_activity


class LastUserInputChannelSort(RecentChannelSort):
    title = "Last user input"

    @staticmethod
    def timestamp(view: ChannelView) -> float:
        return view.last_user_input
