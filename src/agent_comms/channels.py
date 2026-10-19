"""Persistent tag vocabulary and named tag views, shared by every adapter."""

from __future__ import annotations

import time
from abc import abstractmethod
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from .channel_targets import ChannelTarget, Tag
from .cli_result import CliResult
from .declared_family import DeclaredFamily
from .display_order import CreatedThreadSort, ThreadSort
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .messages import MembershipChange
from .history_result import ChannelHistoryPageResult, EverythingHistoryPageResult, HistoryPageResult
from .read_basis import AllChannelDisplayScope, ChannelDisplayScope, MemberActivityDisplayScope
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .registry_document import RosterVisibility
    from .registry_provenance import RegistryProvenance
    from .threads import Thread


class ChannelMemberIndex(dict[str, tuple["Thread", ...]]):
    """Executable threads indexed once for one roster, including empty tags."""

    __slots__ = ("threads",)

    def __init__(self, threads: Iterable[Thread]):
        by_tag: dict[str, list[Thread]] = defaultdict(list)
        executable = []
        for executor in (member for thread in threads for member in thread.role.executor(thread)):
            executable.append(executor)
            for tag in executor.tags:
                by_tag[tag].append(executor)
        self.threads = tuple(executable)
        super().__init__((tag, tuple(members)) for tag, members in by_tag.items())

    def __missing__(self, tag: str) -> tuple[Thread, ...]:
        return ()


@dataclass(frozen=True, slots=True)
class Channel(DeclaredFamily, CliResult, ToolResult, affix="Channel"):
    """A captured channel presentation with behavior supplied by its declaration."""

    name: str
    tags: frozenset[str] = frozenset()
    order: type[ThreadSort] = CreatedThreadSort
    created_at: float = 0
    pinned: bool = False
    parent: str | None = None
    archived: bool = False
    any_mode: bool = False
    history_result_type: ClassVar[type[HistoryPageResult]] = ChannelHistoryPageResult

    def __post_init__(self) -> None:
        name = ChannelTarget.normalize_name(self.name)
        object.__setattr__(self, "name", name)
        if self.parent is not None:
            object.__setattr__(self, "parent", ChannelTarget.normalize_name(self.parent))
        if not isinstance(self.order, type) or self.order not in ThreadSort.members_with(ThreadSort):
            raise ValueError("Channel ordering must be a declared ThreadSort.")
        if not isinstance(self.archived, bool) or not isinstance(self.any_mode, bool):
            raise ValueError("Channel presentation flags must be boolean.")
        if self.any_mode and not self.exact:
            raise ValueError("Only exact tag channels can include member activity.")
        if self.parent is not None:
            ChannelTarget(self.parent).validate()
        if self.parent == name:
            raise RelationViolationError("A channel cannot be its own parent.")
        self.validate_declaration()

    @abstractmethod
    def validate_declaration(self) -> None: ...

    @abstractmethod
    def matches(self, tags: frozenset[str]) -> bool: ...

    @abstractmethod
    def members(self, index: ChannelMemberIndex) -> tuple[Thread, ...]: ...

    @property
    @abstractmethod
    def exact(self) -> bool: ...

    @property
    @abstractmethod
    def read_only(self) -> bool: ...

    @abstractmethod
    def can_set_archived(self, archived: bool) -> bool: ...

    @classmethod
    def require_routable(cls) -> None:
        pass

    @classmethod
    def exact_stored_target(cls) -> bool:
        return True

    @abstractmethod
    def history_scope(self) -> ChannelDisplayScope: ...

    def display_scope(self, snapshot: RegistryProvenance) -> ChannelDisplayScope:
        return self.history_scope()

    @abstractmethod
    def require_metadata(self) -> None: ...

    @abstractmethod
    def require_parent(self) -> None: ...

    @abstractmethod
    def can_restore_preferences(self, existing: bool) -> bool: ...

    @abstractmethod
    def sort_key(self, ordering, view) -> tuple: ...

    def delete_view_bindings(self, command) -> tuple:
        return ()

    @abstractmethod
    def membership_changes(self, before: frozenset[str], after: frozenset[str]) -> tuple[MembershipChange, ...]: ...

    def pending_count(self, counts: dict[str, int]) -> int:
        return sum(self.history_scope().target_values(counts))

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode(self)


class Channels(dict[str, Channel]):
    """The captured declarations select the channels shown by a roster."""

    def visible(self, visibility: RosterVisibility) -> Iterator[Channel]:
        return (channel for channel in self.values() if visibility.archived or not channel.archived)


class PredicateChannel:
    """Builtins and saved predicates select from the index's executable set."""

    __slots__ = ()

    def members(self, index: ChannelMemberIndex) -> tuple[Thread, ...]:
        return tuple(thread for thread in index.threads if self.matches(thread.tags))


class GroupedChannel:
    """Tag and saved-view declarations share finite history and presentation metadata."""

    __slots__ = ()

    @property
    @abstractmethod
    def history_targets(self) -> frozenset[str]: ...

    def history_scope(self) -> ChannelDisplayScope:
        return ChannelDisplayScope(self.name, self.history_targets)

    def can_set_archived(self, archived: bool) -> bool:
        return self.archived != archived

    def require_metadata(self) -> None:
        pass

    def require_parent(self) -> None:
        pass

    def can_restore_preferences(self, existing: bool) -> bool:
        return True

    def sort_key(self, ordering, view) -> tuple:
        return not self.pinned, len(BuiltinChannelView.names()), *ordering.sort_fields(view)


class MembershipChannel:
    """Actual membership channels publish joins/leaves, never saved projections."""

    __slots__ = ()

    @property
    def read_only(self) -> bool:
        return False

    def membership_changes(self, before: frozenset[str], after: frozenset[str]) -> tuple[MembershipChange, ...]:
        previous, current = self.matches(before), self.matches(after)
        if previous == current:
            return ()
        return (MembershipChange.JOINED if current else MembershipChange.LEFT,)


class BuiltinChannelView(PredicateChannel, MembershipChannel, Channel, declared_name=None):
    def validate_declaration(self) -> None:
        if self.name != self.declared_name:
            raise ValueError("Built-in channel must retain its declaration identity.")
        if self.tags:
            raise ValueError("Built-in channels cannot have tag filters.")
        if self.parent is not None or self.archived:
            raise ValueError("Built-in channels cannot be grouped or archived.")

    exact = False

    @classmethod
    def matches(cls, tags: frozenset[str]) -> bool:
        return True

    def can_set_archived(self, archived: bool) -> bool:
        return False

    @classmethod
    def history_scope(cls) -> ChannelDisplayScope:
        return ChannelDisplayScope(cls.declared_name, frozenset((cls.declared_name,)))

    def require_metadata(self) -> None:
        raise ValueError("Built-in channels cannot be grouped or archived.")

    def require_parent(self) -> None:
        raise ValueError("Built-in channels cannot be presentation parents.")

    def can_restore_preferences(self, existing: bool) -> bool:
        return not existing

    def sort_key(self, ordering, view) -> tuple:
        return not self.pinned, BuiltinChannelView.names().index(self.name), 0, 0, self.name


class NonRoutableChannel:
    """Views refuse sends and never identify an immutable addressed target."""

    @classmethod
    def require_routable(cls) -> None:
        raise RelationViolationError("The selected view is a projection, not a routable target.")

    @classmethod
    def exact_stored_target(cls) -> bool:
        return False


class AnyChannel(NonRoutableChannel, BuiltinChannelView, declared_name="#any"):
    history_result_type = EverythingHistoryPageResult

    @classmethod
    def history_scope(cls) -> ChannelDisplayScope:
        return AllChannelDisplayScope(cls.declared_name)


class NoneChannel(BuiltinChannelView, declared_name="#none"):
    @classmethod
    def matches(cls, tags: frozenset[str]) -> bool:
        return not tags


class AllChannel(BuiltinChannelView, declared_name="#all"):
    pass


ANY_CHANNEL = ChannelTarget(AnyChannel.declared_name)
"""Every channel a reader receives: the target of an unscoped inbox or count."""


class ReadOnlyChannel(NonRoutableChannel):
    """History projections never acquire routing or membership authority."""

    @property
    def read_only(self) -> bool:
        return True

    def membership_changes(self, before: frozenset[str], after: frozenset[str]) -> tuple[MembershipChange, ...]:
        return ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SavedViewChannel(ReadOnlyChannel, PredicateChannel, GroupedChannel, Channel):
    view: SavedView

    def validate_declaration(self) -> None:
        if self.name != f"#{self.view.name}" or self.tags != self.view.predicate.tags:
            raise ValueError("Saved projection must retain its declaration identity.")

    def matches(self, tags: frozenset[str]) -> bool:
        return self.view.predicate.matches(tags)

    @property
    def exact(self) -> bool:
        return False

    @property
    def history_targets(self) -> frozenset[str]:
        return self.view.history_targets

    def delete_view_bindings(self, command) -> tuple:
        return (command(name=self.view.name),)


@dataclass(frozen=True, slots=True)
class TagChannelIdentity(GroupedChannel, Channel):
    """A tag's original address remains its history identity after retirement."""

    def validate_declaration(self) -> None:
        Tag.validate(self.name.removeprefix("#"))
        if self.tags != frozenset({self.name.removeprefix("#")}):
            raise ValueError("A routable channel must be its exact tag.")

    @property
    def exact(self) -> bool:
        return True

    @property
    def history_targets(self) -> frozenset[str]:
        return frozenset({self.name})


@dataclass(frozen=True, slots=True)
class TagChannel(MembershipChannel, TagChannelIdentity):
    def display_scope(self, snapshot: RegistryProvenance) -> ChannelDisplayScope:
        if self.any_mode:
            return MemberActivityDisplayScope.capture(self, snapshot)
        return self.history_scope()

    def members(self, index: ChannelMemberIndex) -> tuple[Thread, ...]:
        return index[self.name.removeprefix("#")]

    def matches(self, tags: frozenset[str]) -> bool:
        return bool(self.tags & tags)


@dataclass(frozen=True, slots=True)
class RetiredTagChannel(ReadOnlyChannel, TagChannelIdentity):
    def validate_declaration(self) -> None:
        super(RetiredTagChannel, self).validate_declaration()
        if self.any_mode:
            raise ValueError("Retired channels cannot include member activity.")

    def members(self, index: ChannelMemberIndex) -> tuple[Thread, ...]:
        return ()

    def matches(self, tags: frozenset[str]) -> bool:
        return False


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
class SavedView(ToolResult):
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
