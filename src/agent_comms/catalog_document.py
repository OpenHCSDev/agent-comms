"""Catalog declarations and presentation preferences share one durable document."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from .bus_activity_index import ActivityClocks
from . import channels
from .channel_targets import ChannelTarget, DeclaredTag, Tag
from .channels import (
    BuiltinChannelView,
    Channel,
    Channels,
    SavedView,
    SavedViewChannel,
    TagChannel,
    ViewPredicate,
)
from .display_order import ChannelSort, CreatedThreadSort, NameChannelSort, ThreadSort
from .errors import RelationViolationError, UnknownChannelError


@dataclass(frozen=True, slots=True)
class ChannelPreferences:
    order: type[ThreadSort] = CreatedThreadSort
    created_at: float = 0.0
    pinned: bool = False
    parent: str | None = None
    archived: bool = False
    any_mode: bool = False
    pinned_threads: frozenset[str] = frozenset()

    def channel[C: Channel](self, declaration: type[C], name: str, tags: frozenset[str], **identity) -> C:
        return declaration(
            name,
            tags,
            self.order,
            self.created_at,
            self.pinned,
            self.parent,
            self.archived,
            self.any_mode,
            **identity,
        )


@dataclass(slots=True)
class CatalogDocument:
    tags: frozenset[str] = frozenset()
    preferences: dict[str, ChannelPreferences] = field(default_factory=dict)
    saved_views: dict[str, SavedView] = field(default_factory=dict)
    list_order: type[ChannelSort] = NameChannelSort

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for tag in self.tags:
            Tag.validate(tag)
            channel = ChannelTarget.normalize_name(tag)
            if channel not in self.preferences or self.preferences[channel].created_at <= 0:
                raise ValueError(
                    f"Channel {channel} has no recorded creation time; carry its catalog first.")
        if any(name != view.name for name, view in self.saved_views.items()):
            raise ValueError("Saved view key must match its declared name")
        self.validate_parents()

    def validate_parents(self) -> None:
        for name in self.preferences:
            visited = set()
            parent = name
            while parent is not None:
                if parent in visited:
                    raise ValueError("Channel parent relation cannot contain a cycle.")
                visited.add(parent)
                parent = self.preferences.get(parent, ChannelPreferences()).parent

    @property
    def present_tags(self) -> frozenset[str]:
        """The tags this catalog created and every declared tag, which no bus lacks."""
        return self.tags | frozenset(DeclaredTag.names())

    def views(self, activity: ActivityClocks) -> Channels:
        result = Channels((name, self.require_channel(name, activity)) for name in BuiltinChannelView.names())
        result.update((channel, self.require_channel(channel, activity))
                      for channel in map(ChannelTarget.normalize_name,
                                         (*sorted(self.present_tags), *self.saved_views)))
        return result

    def require_channel(self, target: str, activity: ActivityClocks) -> Channel:
        target = ChannelTarget.normalize_name(target)
        tag = ChannelTarget.bare_name(target)
        if target in BuiltinChannelView.names():
            preference = self.preferences.get(target, ChannelPreferences())
            return preference.channel(BuiltinChannelView.member_named(target), target, frozenset())
        if (view := self.saved_views.get(tag)) is not None:
            preference = self.preferences.get(target, ChannelPreferences(created_at=view.created_at))
            return preference.channel(SavedViewChannel, target, view.predicate.tags, view=view)
        if tag in self.present_tags:
            preference = self.preferences.get(target, ChannelPreferences())
            return preference.channel(TagChannel, target, frozenset({tag}))
        if target in activity.channels:
            preference = self.preferences.get(target, ChannelPreferences())
            return preference.channel(channels.RetiredTagChannel, target, frozenset({tag}))
        raise UnknownChannelError(f"Unknown channel: {target!r}")

    def targets_for(self, tags: frozenset[str]) -> frozenset[str]:
        return frozenset(
            kind.declared_name for kind in BuiltinChannelView.members_with(BuiltinChannelView)
            if kind.matches(tags)
        ) | frozenset(map(ChannelTarget.normalize_name, tags))

    def pinned_threads(self, channel: str) -> frozenset[str]:
        return self.preferences.get(channel, ChannelPreferences()).pinned_threads

    def pinned_members(self) -> dict[str, frozenset[str]]:
        return {
            name: value.pinned_threads
            for name, value in self.preferences.items()
            if value.pinned_threads
        }

    def require_available_name(self, name: str) -> None:
        if (bare := ChannelTarget.bare_name(name)) in self.saved_views:
            raise RelationViolationError(f"Name {bare!r} is reserved by a saved view.")

    def require_available_tag_name(self, name: str) -> None:
        """A new tag's name: free of saved views and of every created tag."""
        self.require_available_name(name)
        if name in self.tags:
            raise RelationViolationError(
                f"Tag {name!r} already exists; tag rename does not merge identities."
            )

    def require_available_rename(self, previous: str, name: str) -> None:
        """A rename's new name: the tag itself, or a name no created or declared tag holds."""
        if name == previous:
            return
        self.require_available_tag_name(name)
        if name in DeclaredTag.names():
            raise RelationViolationError(
                f"Tag {name!r} is declared on every bus; tag rename does not merge identities.")

    def require_changeable_tag(self, tag: str) -> None:
        """A created tag may be renamed or deleted; a declared one is on every bus."""
        if tag in DeclaredTag.names():
            raise RelationViolationError(
                f"Tag {tag!r} is declared on every bus; it cannot be renamed or deleted.")
        if tag not in self.tags:
            raise UnknownChannelError(f"Unknown channel: {ChannelTarget.normalize_name(tag)}")

    def remember_tags(self, tags: frozenset[str]) -> None:
        created_at = time.time()
        for tag in tags - self.tags:
            Tag.validate(tag)
            self.preferences[ChannelTarget.normalize_name(tag)] = ChannelPreferences(
                created_at=created_at)
        self.tags |= tags

    def create_tag(self, tag: str) -> None:
        self.require_available_name(tag)
        self.remember_tags(frozenset({tag}))

    def set_view(self, view: SavedView, activity: ActivityClocks) -> None:
        unknown = view.predicate.tags - self.present_tags
        if unknown:
            raise ValueError(f"Unknown view tags: {', '.join(sorted(unknown))}")
        if view.name in self.present_tags or (
            view.name not in self.saved_views
            and ChannelTarget.normalize_name(view.name) in activity.channels
        ):
            raise ValueError(
                f"View name conflicts with channel: {ChannelTarget.normalize_name(view.name)}")
        previous = self.saved_views.get(view.name)
        if previous is not None:
            view = replace(
                view, original_targets=previous.original_targets, created_at=previous.created_at
            )
        elif view.original_targets:
            raise ValueError(
                "Historical target provenance can only come from saved-data migration."
            )
        self.saved_views[view.name] = view

    def delete_view(self, name: str) -> None:
        name = ChannelTarget.bare_name(name)
        if name not in self.saved_views:
            raise ValueError(f"Unknown view: {name!r}")
        if self.saved_views[name].original_targets:
            raise ValueError(
                "Imported history views retain retired targets; archive the view instead."
            )
        del self.saved_views[name]
        self.remove_channel(ChannelTarget.normalize_name(name))

    def set_preferences(self, name: str, activity: ActivityClocks, **changes) -> Channel:
        channel = self.require_channel(name, activity)
        preference = self.preferences.get(channel.name, ChannelPreferences(created_at=channel.created_at))
        self.preferences[channel.name] = replace(preference, **changes)
        return self.require_channel(channel.name, activity)

    def set_metadata(self, name: str, activity: ActivityClocks, *, parent: str | None, archived: bool) -> Channel:
        current = self.require_channel(name, activity)
        current.require_metadata()
        if type(archived) is not bool:
            raise ValueError("Channel archived state must be boolean.")
        if parent is not None:
            parent = ChannelTarget.normalize_name(parent)
            try:
                parent_channel = self.require_channel(parent, activity)
            except UnknownChannelError as error:
                raise ValueError(f"Unknown parent channel: {parent!r}") from error
            parent_channel.require_parent()
        result = self.set_preferences(name, activity, parent=parent, archived=archived)
        self.validate_parents()
        return result

    def set_any_mode(self, name: str, enabled: bool, activity: ActivityClocks) -> Channel:
        if type(enabled) is not bool:
            raise ValueError("Channel any_mode must be boolean.")
        unsupported = ValueError("Only existing exact tag channels support any_mode.")
        try:
            channel = self.require_channel(name, activity)
        except UnknownChannelError as error:
            raise unsupported from error
        if not channel.exact:
            raise unsupported
        return self.set_preferences(name, activity, any_mode=enabled)

    def set_thread_pinned(self, channel: str, thread: str, pinned: bool) -> None:
        preference = self.preferences.get(channel, ChannelPreferences())
        selected = preference.pinned_threads
        selected = selected | {thread} if pinned else selected - {thread}
        self.preferences[channel] = replace(preference, pinned_threads=selected)

    def rename_thread(self, previous: str, current: str) -> None:
        for name, preference in tuple(self.preferences.items()):
            if previous in preference.pinned_threads:
                self.preferences[name] = replace(
                    preference, pinned_threads=(preference.pinned_threads - {previous}) | {current}
                )

    def remove_thread(self, thread: str) -> None:
        for name in tuple(self.preferences):
            self.set_thread_pinned(name, thread, False)

    def remove_channel(self, name: str) -> None:
        self.preferences.pop(name, None)
        self.preferences = {
            child: replace(p, parent=None) if p.parent == name else p
            for child, p in self.preferences.items()
        }

    def change_tag(self, previous: str, current: str | None) -> None:
        self.require_changeable_tag(previous)

        def changed(tags):
            return (tags - {previous}) | ({current} if previous in tags and current else set())

        old_target = ChannelTarget.normalize_name(previous)
        new_target = ChannelTarget.normalize_name(current) if current else None
        if new_target:
            preference = self.preferences.pop(old_target, None)
            if preference is not None:
                self.preferences[new_target] = preference
            self.preferences = {
                child: replace(p, parent=new_target) if p.parent == old_target else p
                for child, p in self.preferences.items()
            }
            self.saved_views = {
                name: replace(
                    view,
                    predicate=ViewPredicate(view.predicate.match, changed(view.predicate.tags)),
                )
                for name, view in self.saved_views.items()
            }
        else:
            self.remove_channel(old_target)
        self.tags = changed(self.tags)

    def restore_missing(self, source: CatalogDocument, activity: ActivityClocks, *, existing: bool) -> tuple[str, ...]:
        declarations = source.views(activity)
        missing = {
            name: channel
            for name, channel in declarations.items()
            if name not in self.preferences and channel.can_restore_preferences(existing)
        }
        for name, channel in missing.items():
            self.preferences[name] = source.preferences.get(
                name, ChannelPreferences(order=channel.order, created_at=channel.created_at)
            )
        self.tags |= source.tags
        self.saved_views = source.saved_views | {
            name: replace(
                view,
                original_targets=view.original_targets | source.saved_views[name].original_targets,
            )
            if name in source.saved_views
            else view
            for name, view in self.saved_views.items()
        }
        if not existing:
            self.list_order = source.list_order
        self.validate_parents()
        return tuple(missing)
