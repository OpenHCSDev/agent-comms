"""Persistent tag vocabulary and named tag views, shared by every adapter."""

import json
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from .declarations import (
    BuiltinChannel,
    Channel,
    ChannelSort,
    SavedView,
    Tag,
    Thread,
    ThreadRegistry,
    ThreadSort,
    ViewPredicate,
    _atomic_write_text,
    _store_lock,
    file_revision,
)


class ChannelCatalog:
    def __init__(self, path: Path, registry: ThreadRegistry):
        self.path = path
        # Long-lived executors may still write the older catalog schema during
        # a UI-only rollout. Keep new preferences in a catalog-owned sidecar so
        # those writers cannot erase pins while handling unrelated tag changes.
        self.pins_path = path.with_name("channel_pins.json")
        self.metadata_path = path.with_name("channel_metadata.json")
        self.saved_views_path = path.with_name("saved_views.json")
        self.registry = registry
        self._revision: tuple | None = None
        self._tags: frozenset[str] = frozenset()
        self._channels: dict[str, Channel] = {}
        self._saved_views: dict[str, SavedView] = {}
        self._parents: dict[str, str] = {}
        self._archived_channels: frozenset[str] = frozenset()
        self._any_modes: frozenset[str] = frozenset()
        self._orders: dict[str, ThreadSort] = {}
        self._created_at: dict[str, float] = {}
        self._list_order = ChannelSort.NAME
        self._pinned_channels: frozenset[str] = frozenset()
        self._pinned_threads: dict[str, frozenset[str]] = {}

    def read(self) -> tuple[frozenset[str], dict[str, Channel]]:
        with _store_lock(self.path):
            revision = (
                file_revision(self.path),
                file_revision(self.pins_path),
                file_revision(self.metadata_path),
                file_revision(self.saved_views_path),
            )
            if revision != self._revision:
                raw = json.loads(self.path.read_text()) if revision[0] else {}
                pins = json.loads(self.pins_path.read_text()) if revision[1] else {}
                metadata = json.loads(self.metadata_path.read_text()) if revision[2] else {}
                saved_views = json.loads(self.saved_views_path.read_text()) if revision[3] else {}
                tags = frozenset(Tag(tag).name for tag in raw.get("tags", []))
                self._orders = {
                    name: ThreadSort(value) for name, value in raw.get("orders", {}).items()
                }
                self._created_at = {
                    name: float(value) for name, value in raw.get("created_at", {}).items()
                }
                self._list_order = ChannelSort(raw.get("list_order", ChannelSort.NAME.value))
                self._pinned_channels = frozenset(pins.get("channels", []))
                self._pinned_threads = {
                    name: frozenset(members) for name, members in pins.get("threads", {}).items()
                }
                channel_metadata = metadata.get("channels", {})
                self._parents = {
                    name: str(value["parent"])
                    for name, value in channel_metadata.items()
                    if value.get("parent") is not None
                }
                self._archived_channels = frozenset(
                    name for name, value in channel_metadata.items() if value.get("archived", False)
                )
                if any(
                    type(value.get("any_mode", False)) is not bool
                    for value in channel_metadata.values()
                ):
                    raise ValueError("Channel any_mode must be boolean.")
                self._any_modes = frozenset(
                    name for name, value in channel_metadata.items() if value.get("any_mode", False)
                )
                channels = {
                    name: Channel(
                        name,
                        frozenset(tags),
                        self._orders.get(name, ThreadSort.CREATED),
                        self._created_at.get(name, 0),
                        name in self._pinned_channels,
                        self._parents.get(name),
                        name in self._archived_channels,
                        name in self._any_modes,
                    )
                    for name, tags in raw.get("channels", {}).items()
                }
                views = {
                    name: SavedView.from_wire(name, value)
                    for name, value in saved_views.get("views", {}).items()
                }
                self._tags, self._channels, self._saved_views, self._revision = (
                    tags,
                    channels,
                    views,
                    revision,
                )
            return self._tags, dict(self._channels)

    def write(
        self,
        tags: frozenset[str],
        channels: Mapping[str, Channel],
        orders: Mapping[str, ThreadSort] | None = None,
    ) -> None:
        # Mutating operations also hold the wire lock across registry/catalog writes.
        with _store_lock(self.path):
            saved_orders = dict(self._orders if orders is None else orders)
            saved_orders.update({name: channel.order for name, channel in channels.items()})
            threads = tuple(self.registry.all_threads().values())
            all_tags = tags.union(*(thread.tags for thread in threads))
            created = {
                f"#{tag}": self._created_at.get(
                    f"#{tag}",
                    min(
                        (thread.created_at for thread in threads if tag in thread.tags),
                        default=time.time(),
                    ),
                )
                for tag in sorted(all_tags)
            }
            created.update(
                {
                    name: self._created_at.get(
                        name,
                        self._channels[name].created_at if name in self._channels else time.time(),
                    )
                    for name in channels
                }
            )
            _atomic_write_text(
                self.path,
                json.dumps(
                    {
                        "tags": sorted(tags),
                        "created_at": created,
                        "list_order": self._list_order.value,
                        "orders": {name: order.value for name, order in saved_orders.items()},
                        "channels": {
                            name: sorted(channel.tags) for name, channel in channels.items()
                        },
                    },
                    indent=2,
                ),
            )
            _atomic_write_text(
                self.pins_path,
                json.dumps(
                    {
                        "channels": sorted(self._pinned_channels),
                        "threads": {
                            name: sorted(members)
                            for name, members in sorted(self._pinned_threads.items())
                            if members
                        },
                    },
                    indent=2,
                ),
            )
            _atomic_write_text(self.metadata_path, self._metadata_text(self._any_modes))
            _atomic_write_text(
                self.saved_views_path,
                json.dumps(
                    {
                        "views": {
                            name: {
                                key: value for key, value in view.to_wire().items() if key != "name"
                            }
                            for name, view in sorted(self._saved_views.items())
                        }
                    },
                    indent=2,
                ),
            )

    def _metadata_text(self, modes: frozenset[str]) -> str:
        names = sorted(set(self._parents) | set(self._archived_channels) | set(modes))
        return json.dumps(
            {
                "channels": {
                    name: {
                        "parent": self._parents.get(name),
                        "archived": name in self._archived_channels,
                        **({"any_mode": True} if name in modes else {}),
                    }
                    for name in names
                }
            },
            indent=2,
        )

    def tags(self) -> frozenset[str]:
        tags, channels = self.read()
        return tags.union(
            *(thread.tags for thread in self.registry.all_threads().values()),
            *(channel.tags for channel in channels.values()),
        )

    def views(self, threads: Mapping[str, Thread] | None = None) -> dict[str, Channel]:
        """Return routable channels; saved projections are intentionally separate."""
        tags, explicit = self.read()
        threads = self.registry.all_threads() if threads is None else threads
        tags = tags.union(*(thread.tags for thread in threads.values()))
        automatic = {
            f"#{tag}": Channel(
                f"#{tag}",
                frozenset({tag}),
                self._orders.get(f"#{tag}", ThreadSort.CREATED),
                self._created_at.get(
                    f"#{tag}",
                    min(
                        (thread.created_at for thread in threads.values() if tag in thread.tags),
                        default=0,
                    ),
                ),
                f"#{tag}" in self._pinned_channels,
                self._parents.get(f"#{tag}"),
                f"#{tag}" in self._archived_channels,
                f"#{tag}" in self._any_modes,
            )
            for tag in sorted(tags)
        }
        return {
            **{
                kind.value: Channel(
                    kind.value,
                    order=self._orders.get(kind.value, ThreadSort.CREATED),
                    pinned=kind.value in self._pinned_channels,
                )
                for kind in BuiltinChannel
            },
            **explicit,
            # Exact tag channels are never suppressed by a compatibility union.
            **automatic,
        }

    def saved_views(self) -> dict[str, SavedView]:
        self.read()
        return dict(self._saved_views)

    def set_view(self, view: SavedView) -> SavedView:
        tags, channels = self.read()
        unknown = view.predicate.tags - self.tags()
        if unknown:
            raise ValueError(f"Unknown view tags: {', '.join(sorted(unknown))}")
        if f"#{view.name}" in self.views():
            raise ValueError(f"View name conflicts with channel: #{view.name}")
        self._saved_views[view.name] = view
        self.write(tags, channels)
        return view

    def delete_view(self, name: str) -> None:
        tags, channels = self.read()
        canonical = name.removeprefix("#")
        if canonical not in self._saved_views:
            raise ValueError(f"Unknown view: {canonical!r}")
        del self._saved_views[canonical]
        self.write(tags, channels)

    def is_view_target(self, target: str) -> bool:
        self.read()
        return target.startswith("#") and target.removeprefix("#") in self._saved_views

    @property
    def list_order(self) -> ChannelSort:
        self.read()
        return self._list_order

    def set_list_order(self, order: ChannelSort) -> ChannelSort:
        tags, channels = self.read()
        self._list_order = order
        self.write(tags, channels)
        return order

    def remember_tags(self, tags: frozenset[str], created_at: float) -> None:
        known, channels = self.read()
        if all(f"#{tag}" in self._created_at for tag in tags):
            return
        for tag in tags:
            self._created_at.setdefault(f"#{tag}", created_at)
        self.write(known, channels)

    def set_order(self, name: str, order: ThreadSort) -> Channel:
        tags, channels = self.read()
        views = self.views()
        name = name if name.startswith("#") else f"#{name}"
        if name not in views:
            raise ValueError(f"Unknown channel: {name!r}")
        channel = replace(views[name], order=order)
        if name in channels:
            channels[name] = channel
        self.write(tags, channels, {**self._orders, name: order})
        return channel

    def set_metadata(self, name: str, *, parent: str | None, archived: bool) -> Channel:
        tags, channels = self.read()
        name = name if name.startswith("#") else f"#{name}"
        current = self.views()
        channel = current.get(name)
        if channel is None:
            raise ValueError(f"Unknown channel: {name!r}")
        if channel.builtin is not None:
            raise ValueError("Built-in channels cannot be grouped or archived.")
        if not isinstance(archived, bool):
            raise ValueError("Channel archived state must be boolean.")
        if parent is not None:
            parent = parent if parent.startswith("#") else f"#{parent}"
            if parent not in current:
                raise ValueError(f"Unknown parent channel: {parent!r}")
            if current[parent].builtin is not None:
                raise ValueError("Built-in channels cannot be presentation parents.")
            cursor: str | None = parent
            while cursor is not None:
                if cursor == name:
                    raise ValueError("Channel parent relation cannot contain a cycle.")
                cursor = self._parents.get(cursor)
        if parent is None:
            self._parents.pop(name, None)
        else:
            self._parents[name] = parent
        archived_names = set(self._archived_channels)
        (archived_names.add if archived else archived_names.discard)(name)
        self._archived_channels = frozenset(archived_names)
        self.write(tags, channels)
        return self.resolve(name)

    def set_any_mode(self, name: str, enabled: bool) -> Channel:
        """Persist a local display preference, never a delivery or access rule."""
        if not isinstance(enabled, bool):
            raise ValueError("Channel any_mode must be boolean.")
        self.read()
        name = name if name.startswith("#") else f"#{name}"
        channel = self.views().get(name)
        if channel is None or not channel.exact or channel.builtin is not None:
            raise ValueError("Only existing exact tag channels support any_mode.")
        if channel.any_mode == enabled:
            return channel
        selected = set(self._any_modes)
        (selected.add if enabled else selected.discard)(name)
        modes = frozenset(selected)
        expected_metadata_revision = self._revision[2] if self._revision is not None else None
        # A toggle publishes only its one atomic sidecar; it cannot fail after
        # persisting the mode because an unrelated catalog file write failed.
        with _store_lock(self.path):
            if file_revision(self.metadata_path) != expected_metadata_revision:
                self._revision = None
                raise RuntimeError("Channel metadata changed; retry the local preference update.")
            _atomic_write_text(self.metadata_path, self._metadata_text(modes))
        self._any_modes = modes
        self._revision = None
        return self.resolve(name)

    def set_pinned(self, name: str, pinned: bool) -> Channel:
        tags, channels = self.read()
        name = name if name.startswith("#") else f"#{name}"
        if name not in self.views():
            raise ValueError(f"Unknown channel: {name!r}")
        selected = set(self._pinned_channels)
        (selected.add if pinned else selected.discard)(name)
        self._pinned_channels = frozenset(selected)
        self.write(tags, channels)
        return self.resolve(name)

    def pinned_threads(self, channel: str) -> frozenset[str]:
        self.read()
        return self._pinned_threads.get(channel, frozenset())

    def pinned_threads_snapshot(self) -> dict[str, frozenset[str]]:
        """Copy the presentation pin basis without one sidecar read per view."""
        self.read()
        return dict(self._pinned_threads)

    def set_thread_pinned(self, channel: str, thread: str, pinned: bool) -> None:
        tags, channels = self.read()
        selected = set(self._pinned_threads.get(channel, frozenset()))
        (selected.add if pinned else selected.discard)(thread)
        if selected:
            self._pinned_threads[channel] = frozenset(selected)
        else:
            self._pinned_threads.pop(channel, None)
        self.write(tags, channels)

    def rename_thread(self, previous: str, current: str) -> None:
        tags, channels = self.read()
        changed = False
        for name, members in tuple(self._pinned_threads.items()):
            if previous in members:
                self._pinned_threads[name] = (members - {previous}) | {current}
                changed = True
        if changed:
            self.write(tags, channels)

    def remove_thread(self, thread: str) -> None:
        tags, channels = self.read()
        changed = False
        for name, members in tuple(self._pinned_threads.items()):
            if thread not in members:
                continue
            remaining = members - {thread}
            if remaining:
                self._pinned_threads[name] = remaining
            else:
                del self._pinned_threads[name]
            changed = True
        if changed:
            self.write(tags, channels)

    def remove_channel(self, name: str, *, remove_metadata: bool = True) -> None:
        """Discard preferences only when the underlying channel identity is removed."""
        if not remove_metadata:
            return
        self._pinned_channels = self._pinned_channels - {name}
        self._pinned_threads.pop(name, None)
        self._parents.pop(name, None)
        self._parents = {child: parent for child, parent in self._parents.items() if parent != name}
        self._archived_channels = self._archived_channels - {name}
        self._any_modes = self._any_modes - {name}
        self._orders.pop(name, None)
        self._created_at.pop(name, None)

    def change_tag_metadata(self, previous: str, current: str | None) -> None:
        """Move presentation/view references when the membership authority changes."""
        previous_channel = f"#{previous}"
        current_channel = f"#{current}" if current else None
        parent = self._parents.pop(previous_channel, None)
        if current_channel is not None and parent is not None:
            self._parents.setdefault(current_channel, parent)
        if current_channel is None:
            self._parents = {
                child: parent
                for child, parent in self._parents.items()
                if parent != previous_channel
            }
        else:
            self._parents = {
                child: (current_channel if parent == previous_channel else parent)
                for child, parent in self._parents.items()
            }
        archived = set(self._archived_channels)
        if previous_channel in archived:
            archived.remove(previous_channel)
            if current_channel is not None:
                archived.add(current_channel)
        self._archived_channels = frozenset(archived)
        modes = set(self._any_modes)
        if previous_channel in modes:
            modes.remove(previous_channel)
            if current_channel is not None:
                modes.add(current_channel)
        self._any_modes = frozenset(modes)

        if current is not None:
            self._saved_views = {
                name: replace(
                    view,
                    predicate=ViewPredicate(
                        view.predicate.match,
                        (view.predicate.tags - {previous})
                        | ({current} if previous in view.predicate.tags else set()),
                    ),
                )
                for name, view in self._saved_views.items()
            }

    def require_unreferenced_tag(self, tag: str) -> None:
        self.read()
        references = sorted(
            name for name, view in self._saved_views.items() if tag in view.predicate.tags
        )
        if references:
            raise ValueError(
                f"Tag {tag!r} is referenced by saved views: {', '.join(references)}; "
                "update or delete those views first."
            )

    def require_available_name(self, name: str) -> None:
        self.read()
        canonical = name.removeprefix("#")
        if canonical in self._saved_views:
            raise ValueError(f"Name {canonical!r} is reserved by a saved view.")

    def require_available_tag_name(self, name: str, *, previous: str | None = None) -> None:
        tags, channels = self.read()
        self.require_available_name(name)
        if name in self.tags() and name != previous:
            raise ValueError(f"Tag {name!r} already exists; tag rename does not merge identities.")
        target = f"#{name}"
        if target in channels and name != previous:
            raise ValueError(
                f"Tag {name!r} conflicts with legacy channel target {target!r}; "
                "delete or rename that audience first."
            )

    def rename_tag_channel(self, previous: str, current: str) -> None:
        """Move one exact channel's presentation preferences without merging."""
        if previous in self._pinned_channels:
            self._pinned_channels = (self._pinned_channels - {previous}) | {current}
        if previous in self._pinned_threads:
            self._pinned_threads[current] = self._pinned_threads.pop(previous)
        if previous in self._orders:
            self._orders[current] = self._orders.pop(previous)
        else:
            self._orders.pop(current, None)
        if previous in self._created_at:
            self._created_at[current] = self._created_at.pop(previous)
        else:
            self._created_at.pop(current, None)

    def resolve(self, target: str) -> Channel:
        target = "#all" if target == "broadcast" else target
        tags, explicit = self.read()
        if BuiltinChannel.lookup(target):
            return Channel(
                target,
                order=self._orders.get(target, ThreadSort.CREATED),
                pinned=target in self._pinned_channels,
            )
        tag = target.removeprefix("#")
        if tag not in tags and target in explicit:
            return explicit[target]
        return Channel(
            target,
            frozenset({tag}),
            self._orders.get(target, ThreadSort.CREATED),
            self._created_at.get(target, 0),
            target in self._pinned_channels,
            self._parents.get(target),
            target in self._archived_channels,
            target in self._any_modes,
        )

    def targets_for(self, tags: frozenset[str]) -> frozenset[str]:
        _, explicit = self.read()
        exact = frozenset(f"#{tag}" for tag in tags)
        return (
            frozenset({"broadcast", *(kind.value for kind in BuiltinChannel if kind.matches(tags))})
            | exact
            | frozenset(name for name, channel in explicit.items() if channel.matches(tags))
        )

    def history_targets(self, target: str) -> frozenset[str] | None:
        """None denotes the complete wire; every other history is target-owned."""
        channel = self.resolve(target)
        if channel.builtin is not None:
            return channel.builtin.history_targets
        return frozenset({channel.name})
