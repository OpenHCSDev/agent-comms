"""Channel targets: declaration and persistence owners."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BuiltinChannel(StrEnum):
    ANY = "#any"
    NONE = "#none"
    ALL = "#all"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("broadcast",) if self is self.ALL else ()

    @property
    def names(self) -> frozenset[str]:
        return frozenset((self.value, *self.aliases))

    @classmethod
    def lookup(cls, name: str) -> BuiltinChannel | None:
        return next((channel for channel in cls if name in channel.names), None)

    @classmethod
    def canonical(cls, name: str) -> str:
        channel = cls.lookup(name)
        return channel.value if channel is not None else name

    @classmethod
    def is_alias(cls, name: str) -> bool:
        channel = cls.lookup(name)
        return channel is not None and name != channel.value

    @classmethod
    def exact_stored_target(cls, name: str) -> bool:
        channel = cls.lookup(name)
        return channel is None or (not channel.aggregate and name == channel.value)

    @classmethod
    def aggregate_target(cls, name: str) -> bool:
        channel = cls.lookup(name)
        return channel is not None and channel.aggregate

    @property
    def aggregate(self) -> bool:
        return self is self.ANY

    def matches(self, tags: frozenset[str]) -> bool:
        return self is not self.NONE or not tags

    @property
    def history_targets(self) -> frozenset[str] | None:
        return None if self.aggregate else self.names


_TAG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


def is_channel_target(target: str) -> bool:
    """A ``#``-prefixed channel (``#all`` is the global channel)."""
    return target.startswith("#")


def channel_tag(target: str) -> str:
    """Tag name behind a ``#``-channel target. ``#all`` -> ``all``."""
    return target[1:]


@dataclass(frozen=True, slots=True)
class Tag:
    name: str

    def __post_init__(self) -> None:
        if (
            not self.name
            or not set(self.name) <= _TAG_CHARS
            or BuiltinChannel.lookup(f"#{self.name}")
        ):
            raise ValueError(
                "Tags must be lowercase alphanumeric with hyphens/underscores; "
                "built-in channel names are reserved."
            )
