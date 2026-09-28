"""Persistent tag vocabulary and named tag views, shared by every adapter."""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from .channel_targets import _TAG_CHARS, BuiltinChannel, Tag
from .declared_family import DeclaredFamily
from .display_order import ThreadSort
from .errors import RelationViolationError
from .threads import Thread


@dataclass(frozen=True, slots=True)
class Channel:
    """One routable target and its presentation-only metadata.

    Legacy declarations may still carry several tags as a compatibility
    audience. Exact one-tag channels are always projected independently by the
    catalog; ``parent`` and ``archived`` never participate in matching.
    """

    name: str
    tags: frozenset[str] = frozenset()
    order: ThreadSort = field(default_factory=lambda: ThreadSort.CREATED)
    created_at: float = 0
    pinned: bool = False
    parent: str | None = None
    archived: bool = False
    any_mode: bool = False

    def __post_init__(self) -> None:
        name = self.name if self.name.startswith("#") else f"#{self.name}"
        object.__setattr__(self, "name", name)
        if self.parent is not None:
            parent = self.parent if self.parent.startswith("#") else f"#{self.parent}"
            object.__setattr__(self, "parent", parent)
        if not isinstance(self.order, ThreadSort):
            raise ValueError("Channel ordering must be a declared ThreadSort.")
        if not isinstance(self.archived, bool) or not isinstance(self.any_mode, bool):
            raise ValueError("Channel presentation flags must be boolean.")
        if self.any_mode and not self.exact:
            raise ValueError("Only exact tag channels can include member activity.")
        if not name[1:] or not set(name[1:]) <= _TAG_CHARS:
            raise ValueError(
                "Channel names must be lowercase alphanumeric with hyphens/underscores."
            )
        if self.parent is not None and (
            not self.parent[1:] or not set(self.parent[1:]) <= _TAG_CHARS
        ):
            raise ValueError(
                "Channel parents must be lowercase alphanumeric with hyphens/underscores."
            )
        if self.parent == name:
            raise RelationViolationError("A channel cannot be its own parent.")
        if self.builtin is not None:
            if self.tags:
                raise ValueError("Built-in channels cannot have tag filters.")
            if self.parent is not None or self.archived:
                raise ValueError("Built-in channels cannot be grouped or archived.")
        elif not self.tags:
            raise ValueError("A channel requires at least one tag.")
        for tag in self.tags:
            Tag(tag)

    def matches(self, tags: frozenset[str]) -> bool:
        return self.builtin.matches(tags) if self.builtin else bool(self.tags & tags)

    @property
    def builtin(self) -> BuiltinChannel | None:
        return BuiltinChannel.lookup(self.name)

    @classmethod
    def aggregate_target(cls, name: str) -> bool:
        channel = cls.lookup(name)
        return channel is not None and channel.aggregate

    @classmethod
    def members_for(cls, name: str, threads: Mapping[str, Thread]) -> tuple[str, ...] | None:
        channel = cls.lookup(name)
        if channel is None:
            return None
        return tuple(thread.name for thread in threads.values() if channel.matches(thread.tags))

    @property
    def aggregate(self) -> bool:
        return self.builtin is not None and self.builtin.aggregate

    @property
    def exact(self) -> bool:
        return self.builtin is None and self.tags == frozenset({self.name.removeprefix("#")})

    def to_wire(self) -> dict[str, object]:
        return {**asdict(self), "tags": sorted(self.tags), "order": self.order.value}


class ViewKind(StrEnum):
    PARTICIPANTS = "participants"
    ACTIVITY = "activity"


class ViewMatch(DeclaredFamily, affix="Match"):
    """A tag relation, selected once at the external boundary."""

    @staticmethod
    @abstractmethod
    def matches(required: frozenset[str], observed: frozenset[str]) -> bool:
        """Whether the observed tags satisfy this relation."""


class AnyOfMatch(ViewMatch):
    @staticmethod
    def matches(required: frozenset[str], observed: frozenset[str]) -> bool:
        return bool(required & observed)


class AllOfMatch(ViewMatch):
    @staticmethod
    def matches(required: frozenset[str], observed: frozenset[str]) -> bool:
        return required <= observed


@dataclass(frozen=True, slots=True)
class ViewPredicate:
    """A typed tag predicate; expression syntax is deliberately unsupported."""

    match: type[ViewMatch]
    tags: frozenset[str]

    def __post_init__(self) -> None:
        if not self.tags:
            raise ValueError("A view predicate requires at least one tag.")
        for tag in self.tags:
            Tag(tag)

    def matches(self, tags: frozenset[str]) -> bool:
        return self.match.matches(self.tags, tags)


@dataclass(frozen=True, slots=True)
class SavedView:
    """A named non-routable projection over authoritative channels or activity."""

    name: str = field(metadata={"catalog_exclude": True})
    kind: ViewKind
    predicate: ViewPredicate
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if (
            not self.name
            or self.name.startswith("#")
            or not set(self.name) <= _TAG_CHARS
            or BuiltinChannel.lookup(f"#{self.name}")
        ):
            raise ValueError(
                "View names must be lowercase alphanumeric with hyphens/underscores "
                "and cannot be channel targets."
            )
