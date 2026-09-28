"""Display order: declaration and persistence owners."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Self

from .channel_targets import BuiltinChannel

if TYPE_CHECKING:
    from .presentation import ChannelView


class DisplayOrder(Enum):
    """A persisted sorting choice with its adapter-independent display label."""

    def __new__(cls, key: str, label: str = "") -> Self:
        member = object.__new__(cls)
        member._value_ = key
        member.label = label
        return member

    label: str


class ThreadSort(DisplayOrder):
    LAST_MESSAGE = "last_message_sent", "Last message sent"
    CREATED = "created_at", "Date created"
    LAST_ACTIVITY = "last_activity", "Last activity"

    @classmethod
    def resolve(cls, value: str) -> ThreadSort:
        try:
            return cls(value)
        except ValueError:
            return cls.CREATED

    def key(
        self, name: str, created: float, activity: float, sent: float
    ) -> tuple[float, float, str]:
        timestamp = {
            self.CREATED: created,
            self.LAST_ACTIVITY: activity,
            self.LAST_MESSAGE: sent,
        }[self]
        return -timestamp, -created, name.casefold()


class ChannelSort(DisplayOrder):
    NAME = "name", "Name"
    CREATED = "created_at", "Date created"
    LAST_ACTIVITY = "last_activity", "Last activity"
    LAST_USER_INPUT = "last_user_input", "Last user input"

    def key(self, view: ChannelView) -> tuple[int, int, float, float, str]:
        builtins = tuple(BuiltinChannel)
        builtin = view.channel.builtin
        if builtin is not None:
            return (
                not view.channel.pinned,
                builtins.index(builtin),
                0,
                0,
                view.channel.name,
            )
        timestamp = {
            self.NAME: 0,
            self.CREATED: view.channel.created_at,
            self.LAST_ACTIVITY: view.last_activity,
            self.LAST_USER_INPUT: view.last_user_input,
        }[self]
        created = 0 if self is self.NAME else view.channel.created_at
        return (
            not view.channel.pinned,
            len(builtins),
            -timestamp,
            -created,
            view.channel.name.casefold(),
        )
