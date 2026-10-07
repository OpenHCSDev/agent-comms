"""Persistent tag vocabulary and named tag views, shared by every adapter."""

from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from .channel_targets import _TAG_CHARS, BuiltinChannel, Tag
from .declared_family import DeclaredFamily
from .display_order import ThreadSort
from .errors import RelationViolationError
from .field_codec import FieldCodec


@dataclass(frozen=True, slots=True)
class Channel:
    """A captured channel presentation, including an optional saved projection.

    Only exact tags and routable builtins admit publication. SavedView owns
    the predicate and historical target provenance of a read-only projection.
    """

    name: str
    tags: frozenset[str] = frozenset()
    order: ThreadSort = field(default_factory=lambda: ThreadSort.CREATED)
    created_at: float = 0
    pinned: bool = False
    parent: str | None = None
    archived: bool = False
    any_mode: bool = False
    view: SavedView | None = None

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
        elif self.view is not None:
            if name != f"#{self.view.name}" or self.tags != self.view.predicate.tags:
                raise ValueError("Saved projection must retain its declaration identity.")
        elif self.tags != frozenset({name.removeprefix("#")}):
            raise ValueError("A routable channel must be its exact tag.")
        for tag in self.tags:
            Tag(tag)

    def matches(self, tags: frozenset[str]) -> bool:
        if self.view is not None:
            return self.view.predicate.matches(tags)
        return self.builtin.matches(tags) if self.builtin else bool(self.tags & tags)

    def can_set_archived(self, archived: bool) -> bool:
        """Only nonbuiltin channels admit a changed archive preference."""
        return self.builtin is None and self.archived != archived

    def in_view(self, *, show_archived: bool = False) -> bool:
        """Archive hides presentation; it does not retire membership or routing."""
        return show_archived or not self.archived

    @property
    def builtin(self) -> BuiltinChannel | None:
        return BuiltinChannel.lookup(self.name)

    @property
    def aggregate(self) -> bool:
        return self.builtin is not None and self.builtin.aggregate

    @property
    def exact(self) -> bool:
        return self.view is None and self.builtin is None

    @property
    def history_targets(self) -> frozenset[str] | None:
        if self.view is not None:
            return self.view.history_targets
        return self.builtin.history_targets if self.builtin else frozenset({self.name})

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode(self)


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
    original_targets: frozenset[str] = frozenset()

    @property
    def history_targets(self) -> frozenset[str]:
        # Original addressed messages remain attached to their immutable target;
        # this is a read projection, never a rewritten message or delivery scope.
        return self.original_targets | frozenset(f"#{tag}" for tag in self.predicate.tags)

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
