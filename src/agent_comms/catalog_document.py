"""Catalog declarations and presentation preferences share one durable document."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from .channel_targets import BuiltinChannel, Tag
from .channels import Channel, SavedView, ViewPredicate
from .display_order import ChannelSort, ThreadSort
from .thread_provenance import ThreadProvenance


@dataclass(frozen=True, slots=True)
class ChannelPreferences:
    order: ThreadSort = ThreadSort.CREATED
    created_at: float = 0.0
    pinned: bool = False
    parent: str | None = None
    archived: bool = False
    any_mode: bool = False
    pinned_threads: frozenset[str] = frozenset()

    def channel(self, name: str, tags: frozenset[str], view: SavedView | None = None) -> Channel:
        return Channel(
            name,
            tags,
            self.order,
            self.created_at,
            self.pinned,
            self.parent,
            self.archived,
            self.any_mode,
            view,
        )


@dataclass(slots=True)
class CatalogDocument:
    tags: frozenset[str] = frozenset()
    preferences: dict[str, ChannelPreferences] = field(default_factory=dict)
    saved_views: dict[str, SavedView] = field(default_factory=dict)
    list_order: ChannelSort = ChannelSort.NAME

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for tag in self.tags:
            Tag(tag)
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

    def all_tags(self, threads: Mapping[str, ThreadProvenance]) -> frozenset[str]:
        return self.tags.union(*(thread.tags for thread in threads.values()))

    def views(self, threads: Mapping[str, ThreadProvenance]) -> dict[str, Channel]:
        tags = self.tags.union(*(thread.tags for thread in threads.values()))
        result = {kind.value: self.resolve(kind.value) for kind in BuiltinChannel}
        for tag in sorted(tags):
            name = f"#{tag}"
            preference = self.preferences.get(name)
            if preference is None:
                preference = ChannelPreferences(
                    created_at=min(
                        (thread.created_at for thread in threads.values() if tag in thread.tags),
                        default=0.0,
                    )
                )
            result[name] = preference.channel(name, frozenset({tag}))
        result.update((f"#{name}", self.resolve(f"#{name}")) for name in self.saved_views)
        return result

    def resolve(self, target: str) -> Channel:
        tag = target.removeprefix("#")
        view = self.saved_views.get(tag)
        members = (
            view.predicate.tags
            if view
            else (frozenset() if BuiltinChannel.lookup(target) else frozenset({tag}))
        )
        preference = self.preferences.get(
            target, ChannelPreferences(created_at=view.created_at if view else 0.0)
        )
        return preference.channel(target, members, view)

    def targets_for(self, tags: frozenset[str]) -> frozenset[str]:
        return frozenset(kind.value for kind in BuiltinChannel if kind.matches(tags)) | frozenset(
            f"#{tag}" for tag in tags
        )

    def history_targets(self, target: str) -> frozenset[str] | None:
        return self.resolve(target).history_targets

    def is_view_target(self, target: str) -> bool:
        return target.startswith("#") and target.removeprefix("#") in self.saved_views

    def pinned_threads(self, channel: str) -> frozenset[str]:
        return self.preferences.get(channel, ChannelPreferences()).pinned_threads

    def pinned_members(self) -> dict[str, frozenset[str]]:
        return {
            name: value.pinned_threads
            for name, value in self.preferences.items()
            if value.pinned_threads
        }

    def require_available_name(self, name: str) -> None:
        if name.removeprefix("#") in self.saved_views:
            raise ValueError(f"Name {name.removeprefix('#')!r} is reserved by a saved view.")

    def require_available_tag_name(
        self, name: str, threads: Mapping[str, ThreadProvenance], *, previous: str | None = None
    ) -> None:
        self.require_available_name(name)
        if name in self.all_tags(threads) and name != previous:
            raise ValueError(f"Tag {name!r} already exists; tag rename does not merge identities.")

    def require_unreferenced_tag(self, tag: str) -> None:
        references = sorted(
            name for name, view in self.saved_views.items() if tag in view.predicate.tags
        )
        if references:
            raise ValueError(
                f"Tag {tag!r} is referenced by saved views: {', '.join(references)}; "
                "update or delete those views first."
            )

    def remember_tags(self, tags: frozenset[str], created_at: float) -> None:
        for tag in tags:
            self.preferences.setdefault(f"#{tag}", ChannelPreferences(created_at=created_at))

    def create_tag(self, tag: str, threads: Mapping[str, ThreadProvenance]) -> None:
        self.require_available_name(tag)
        if tag in self.all_tags(threads):
            return
        self.require_available_tag_name(tag, threads)
        self.tags |= {tag}
        self.remember_tags(frozenset({tag}), time.time())

    def set_view(self, view: SavedView, threads: Mapping[str, ThreadProvenance]) -> None:
        unknown = view.predicate.tags - self.all_tags(threads)
        if unknown:
            raise ValueError(f"Unknown view tags: {', '.join(sorted(unknown))}")
        if view.name in self.all_tags(threads):
            raise ValueError(f"View name conflicts with channel: #{view.name}")
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
        name = name.removeprefix("#")
        if name not in self.saved_views:
            raise ValueError(f"Unknown view: {name!r}")
        if self.saved_views[name].original_targets:
            raise ValueError(
                "Imported history views retain retired targets; archive the view instead."
            )
        del self.saved_views[name]
        self.remove_channel(f"#{name}")

    def set_preferences(self, name: str, threads: Mapping[str, ThreadProvenance], **changes) -> Channel:
        name = name if name.startswith("#") else f"#{name}"
        channel = self.views(threads).get(name)
        if channel is None:
            raise ValueError(f"Unknown channel: {name!r}")
        preference = self.preferences.get(name, ChannelPreferences(created_at=channel.created_at))
        self.preferences[name] = replace(preference, **changes)
        return self.resolve(name)

    def set_metadata(
        self, name: str, threads: Mapping[str, ThreadProvenance], *, parent: str | None, archived: bool
    ) -> Channel:
        name = name if name.startswith("#") else f"#{name}"
        current = self.views(threads)
        if name not in current:
            raise ValueError(f"Unknown channel: {name!r}")
        if current[name].builtin is not None:
            raise ValueError("Built-in channels cannot be grouped or archived.")
        if type(archived) is not bool:
            raise ValueError("Channel archived state must be boolean.")
        if parent is not None:
            parent = parent if parent.startswith("#") else f"#{parent}"
            if parent not in current:
                raise ValueError(f"Unknown parent channel: {parent!r}")
            if current[parent].builtin is not None:
                raise ValueError("Built-in channels cannot be presentation parents.")
        result = self.set_preferences(name, threads, parent=parent, archived=archived)
        self.validate_parents()
        return result

    def set_any_mode(self, name: str, enabled: bool, threads: Mapping[str, ThreadProvenance]) -> Channel:
        if type(enabled) is not bool:
            raise ValueError("Channel any_mode must be boolean.")
        name = name if name.startswith("#") else f"#{name}"
        channel = self.views(threads).get(name)
        if channel is None or not channel.exact or channel.builtin is not None:
            raise ValueError("Only existing exact tag channels support any_mode.")
        return self.set_preferences(name, threads, any_mode=enabled)

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
        def changed(tags):
            return (tags - {previous}) | ({current} if previous in tags and current else set())

        old_target, new_target = f"#{previous}", f"#{current}" if current else None
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

    def restore_missing(
        self, source: CatalogDocument, source_threads: Mapping[str, ThreadProvenance], *, existing: bool
    ) -> tuple[str, ...]:
        missing = {
            name: channel
            for name, channel in source.views(source_threads).items()
            if name not in self.preferences and (channel.builtin is None or not existing)
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
