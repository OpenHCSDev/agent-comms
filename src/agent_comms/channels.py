"""Persistent tag vocabulary and named tag views, shared by every adapter."""

from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from .channel_targets import BuiltinChannel, ChannelTarget, Tag
from .declared_family import DeclaredFamily
from .display_order import CreatedThreadSort, ThreadSort
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
    order: type[ThreadSort] = CreatedThreadSort
    created_at: float = 0
    pinned: bool = False
    parent: str | None = None
    archived: bool = False
    any_mode: bool = False
    view: SavedView | None = None

    def __post_init__(self) -> None:
        name = ChannelTarget.normalize_name(self.name)
        object.__setattr__(self, "name", name)
        if self.parent is not None:
            parent = ChannelTarget.normalize_name(self.parent)
            object.__setattr__(self, "parent", parent)
        if not isinstance(self.order, type) or self.order not in ThreadSort.members_with(ThreadSort):
            raise ValueError("Channel ordering must be a declared ThreadSort.")
        if not isinstance(self.archived, bool) or not isinstance(self.any_mode, bool):
            raise ValueError("Channel presentation flags must be boolean.")
        if self.any_mode and not self.exact:
            raise ValueError("Only exact tag channels can include member activity.")
        ChannelTarget(name).validate()
        if self.parent is not None:
            ChannelTarget(self.parent).validate()
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

    def matches(self, tags: frozenset[str]) -> bool:
        if self.view is not None:
            return self.view.predicate.matches(tags)
        builtin = self.builtin
        return builtin.matches(tags) if builtin is not None else bool(self.tags & tags)

    def can_set_archived(self, archived: bool) -> bool:
        """Only nonbuiltin channels admit a changed archive preference."""
        return self.builtin is None and self.archived != archived

    @property
    def builtin(self) -> BuiltinChannel | None:
        return BuiltinChannel.lookup(self.name)

    @property
    def aggregate(self) -> bool:
        builtin = self.builtin
        return builtin is not None and builtin.aggregate

    def require_routable(self) -> None:
        if self.aggregate or self.view is not None:
            raise RelationViolationError(f"View {self.name!r} is a projection, not a routable target.")

    @property
    def exact(self) -> bool:
        return self.view is None and self.builtin is None

    @property
    def history_targets(self) -> frozenset[str] | None:
        if self.view is not None:
            return self.view.history_targets
        builtin = self.builtin
        return builtin.history_targets if builtin is not None else frozenset({self.name})

    def pending_count(self, counts: dict[str, int]) -> int:
        targets = self.history_targets
        return sum(count for scope, count in counts.items() if targets is None or scope in targets)

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
            Tag.validate(tag)

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
        Tag.validate(self.name)
