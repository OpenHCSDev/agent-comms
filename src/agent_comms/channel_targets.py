"""Channel targets: declaration and persistence owners."""

from __future__ import annotations

from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import ClassVar
from enum import StrEnum


class BuiltinChannel(StrEnum):
    ANY = "#any"
    NONE = "#none"
    ALL = "#all"

    @classmethod
    def lookup(cls, name: str) -> BuiltinChannel | None:
        channel = cls._value2member_map_.get(name)
        # The enum owns value lookup. Added value aliases are not declared
        # channel targets; retain the original exact-value contract.
        return channel if channel is not None and name == channel.value else None

    @classmethod
    def exact_stored_target(cls, name: str) -> bool:
        channel = cls.lookup(name)
        return channel is None or not channel.aggregate

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
        return None if self.aggregate else frozenset((self.value,))


class CommandTarget(ABC):
    """A command destination owns its binding and catalog participation."""

    needs_catalog: ClassVar[bool] = False

    @staticmethod
    def named(name: str | CommandTarget) -> CommandTarget:
        if isinstance(name, CommandTarget):
            return name
        if not isinstance(name, str) or not name:
            raise ValueError("Select a named target")
        return ChannelTarget(name) if is_channel_target(name) else ThreadTarget(name)

    @staticmethod
    def arguments(names: tuple[str, ...], project: str):
        selected = tuple(CommandTarget.named(name) for name in names) or (ProjectTarget(project),)
        return selected[0] if len(selected) == 1 else selected

    @property
    @abstractmethod
    def names(self) -> tuple[str, ...]: ...

    def available(self, snapshot) -> bool:
        return True

    @abstractmethod
    def bindings(self, declaration, comms, channel, snapshot, catalog): ...

    def contexts(self, declaration, channel):
        return declaration.selection_channels(channel, self.name)


@dataclass(frozen=True)
class NamedTarget(CommandTarget):
    name: str

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name,)


class ThreadTarget(NamedTarget):
    def available(self, snapshot) -> bool:
        return snapshot.canonical_name(self.name) in snapshot.threads

    def bindings(self, declaration, comms, channel, snapshot, catalog):
        thread = snapshot.require(self.name)
        return declaration.thread_bindings(comms, thread, snapshot.status(thread.name),
                                           channel, catalog=catalog)


class ChannelTarget(NamedTarget):
    needs_catalog = True

    def bindings(self, declaration, comms, channel, snapshot, catalog):
        return declaration.channel_bindings(comms, catalog.resolve(self.name), snapshot=snapshot)


@dataclass(frozen=True)
class ProjectTarget(CommandTarget):
    project: str

    @property
    def name(self) -> str:
        return self.project

    @property
    def names(self) -> tuple[str, ...]:
        return ()

    def contexts(self, declaration, channel):
        return (None,)

    def bindings(self, declaration, comms, channel, snapshot, catalog):
        return declaration.project_bindings(comms, self.project)


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
