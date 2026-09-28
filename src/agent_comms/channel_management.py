"""Channel mutations and membership transactions."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from .active_route import guard_legacy_root_write
from .channels import ChannelCatalog
from .registration import Registration

if TYPE_CHECKING:
    pass
from .channel_targets import BuiltinChannel, Tag
from .channels import Channel, SavedView
from .display_order import ChannelSort, ThreadSort
from .message_bus import MessageBus
from .messages import MembershipChange, Message, MessageType
from .store_files import _store_lock
from .threads import Thread

_LOG = logging.getLogger(__name__)


class TagAction(Enum):
    LIST = "list"
    CREATE = "create"
    RENAME = "rename"
    DELETE = "delete"

    def apply(
        self, channels: ChannelManagement, name: str = "", new_name: str = ""
    ) -> frozenset[str]:
        match self:
            case self.CREATE:
                channels.create_tag(name)
            case self.RENAME:
                channels.rename_tag(name, new_name)
            case self.DELETE:
                channels.delete_tag(name)
        return channels.catalog.tags()


class ChannelManagement:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus):
        self.root = root
        self.registry = registry
        self.bus = bus
        self._wire_lock_path = root / "wire"
        self.catalog = ChannelCatalog(root / "channels.json", registry)

    def channels(self) -> Sequence[str]:
        return list(self.catalog.views())

    def _require_available_new_tags(self, proposed: frozenset[str]) -> None:
        """Validate every tag identity before any enclosing state mutation."""
        known = self.catalog.tags()
        for tag in proposed - known:
            self.catalog.require_available_tag_name(tag)

    def create_tag(self, name: str) -> Tag:
        tag = Tag(name)
        with _store_lock(self._wire_lock_path):
            self.catalog.require_available_name(tag.name)
            if tag.name in self.catalog.tags():
                return tag
            self.catalog.require_available_tag_name(tag.name)
            tags, channels = self.catalog.read()
            self.catalog.write(tags | {tag.name}, channels)
        return tag

    def _rebase_passive_channel_scope(self, name: str) -> None:
        """Membership changes cut off former scope without claiming input delivery."""
        from .passive_channel_awareness import PassiveChannelAwareness

        awareness = PassiveChannelAwareness(self.root)
        # Even checking for an optional ledger can fail after the membership
        # commit. A failed probe skips the advisory; owner reads stay strict.
        try:
            if not awareness.store.path.exists():
                return
        except (OSError, TypeError, ValueError):
            return
        owner = self.registry.require(name)
        snapshot = self.registry.snapshot()
        # The membership write already committed. Advisory storage is
        # optional; a stale scope row suppresses its next-turn frame.
        with suppress(OSError, TypeError, ValueError):
            awareness.scope_changed(
                owner,
                admission=snapshot.admission_generations[owner.name],
                high_water=self.bus.log.latest_sequence(),
                channels=self.catalog.targets_for(owner.tags),
            )

    def update_tags(
        self, name: str, *, add: frozenset[str] = frozenset(), remove: frozenset[str] = frozenset()
    ) -> Thread:
        for tag in add | remove:
            Tag(tag)
        with guard_legacy_root_write(self.root), _store_lock(self._wire_lock_path):
            self._require_available_new_tags(add)
            thread = self.registry.require(name)
            previous_channels = self.catalog.views()
            updated = replace(thread, tags=(thread.tags | add) - remove)
            self.registry.register(updated, self.registry.status(thread.name))
            updated = self.registry.require(thread.name)
            self.catalog.remember_tags(add, time.time())
            if thread.role.executable and thread.tags != updated.tags:
                channels = {**previous_channels, **self.catalog.views()}
                for channel in channels.values():
                    before, after = channel.matches(thread.tags), channel.matches(updated.tags)
                    if before != after:
                        change = MembershipChange.JOINED if after else MembershipChange.LEFT
                        self.bus.publisher.publish(
                            Message(
                                thread.name,
                                channel.name,
                                f"{thread.name} {change.value} {channel.name}",
                                MessageType.INFO,
                                membership=change,
                            )
                        )
            if thread.tags != updated.tags:
                self._rebase_passive_channel_scope(updated.name)
            return updated

    def set_channel(self, name: str, tags: frozenset[str]) -> Channel:
        with _store_lock(self._wire_lock_path):
            self.catalog.require_available_name(name)
            previous = self.catalog.resolve(name if name.startswith("#") else f"#{name}")
            channel = Channel(name, tags, previous.order)
            if channel.builtin is not None:
                raise ValueError("Built-in channels cannot be changed.")
            name_tag = channel.name.removeprefix("#")
            if name_tag in self.catalog.tags() and channel.tags != frozenset({name_tag}):
                raise ValueError(
                    "A named compatibility audience cannot replace an exact tag channel."
                )
            self._require_available_new_tags(channel.tags)
            known, channels = self.catalog.read()
            channels[channel.name] = channel
            self.catalog.write(known | channel.tags, channels)
            return self.catalog.resolve(channel.name)

    def set_saved_view(self, view: SavedView) -> SavedView:
        with _store_lock(self._wire_lock_path):
            return self.catalog.set_view(view)

    def delete_saved_view(self, name: str) -> None:
        with _store_lock(self._wire_lock_path):
            self.catalog.delete_view(name)

    def set_channel_metadata(self, name: str, *, parent: str | None, archived: bool) -> Channel:
        with _store_lock(self._wire_lock_path):
            return self.catalog.set_metadata(name, parent=parent, archived=archived)

    def set_channel_any_mode(self, name: str, enabled: bool) -> Channel:
        """Local UI preference, not same-user authentication or a routing decision."""
        with _store_lock(self._wire_lock_path):
            return self.catalog.set_any_mode(name, enabled)

    def set_channel_sort(self, name: str, order: ThreadSort) -> Channel:
        with _store_lock(self._wire_lock_path):
            return self.catalog.set_order(name, order)

    def set_channel_order(self, order: ChannelSort) -> ChannelSort:
        """Persist channel-list ordering independently of each channel's members."""
        with _store_lock(self._wire_lock_path):
            return self.catalog.set_list_order(order)

    def set_channel_pinned(self, name: str, pinned: bool) -> Channel:
        with _store_lock(self._wire_lock_path):
            return self.catalog.set_pinned(name, pinned)

    def set_thread_pinned(self, channel: str, name: str, pinned: bool) -> None:
        canonical_channel = channel if channel.startswith("#") else f"#{channel}"
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            view = self.catalog.views().get(canonical_channel)
            if view is None:
                raise ValueError(f"Unknown channel: {canonical_channel!r}")
            if pinned and (
                not view.matches(thread.tags)
                or not thread.role.executable
                or not self.registry.status(thread.name).visible
            ):
                raise ValueError(f"Thread {thread.name!r} is not a member of {canonical_channel}.")
            self.catalog.set_thread_pinned(canonical_channel, thread.name, pinned)

    def delete_channel(self, name: str) -> None:
        canonical = name if name.startswith("#") else f"#{name}"
        if BuiltinChannel.lookup(canonical):
            raise ValueError("Built-in channels cannot be deleted.")
        with _store_lock(self._wire_lock_path):
            tags, channels = self.catalog.read()
            if canonical not in channels:
                raise ValueError("This is an automatic tag view; manage its tag instead.")
            del channels[canonical]
            self.catalog.remove_channel(
                canonical, remove_metadata=canonical.removeprefix("#") not in tags
            )
            self.catalog.write(tags, channels)

    def rename_tag(self, name: str, new_name: str) -> None:
        Tag(new_name)
        if name == new_name:
            if name not in self.catalog.tags():
                raise ValueError(f"Unknown tag: {name!r}")
            return
        self._change_tag(name, new_name)

    def delete_tag(self, name: str) -> None:
        self._change_tag(name, None)

    def _change_tag(self, name: str, replacement: str | None) -> None:
        Tag(name)
        with _store_lock(self._wire_lock_path):
            if name not in self.catalog.tags():
                raise ValueError(f"Unknown tag: {name!r}")
            if replacement is None:
                self.catalog.require_unreferenced_tag(name)
            else:
                self.catalog.require_available_tag_name(replacement, previous=name)

            def changed(tags: frozenset[str]) -> frozenset[str]:
                return (tags - {name}) | ({replacement} if name in tags and replacement else set())

            for thread in self.registry.all_threads().values():
                if name in thread.tags:
                    self.registry.register(
                        replace(thread, tags=changed(thread.tags)),
                        self.registry.status(thread.name),
                    )
            tags, channels = self.catalog.read()
            updated_channels: dict[str, Channel] = {}
            previous_target = f"#{name}"
            replacement_target = f"#{replacement}" if replacement else None
            for key, channel in channels.items():
                if selected := changed(channel.tags):
                    target = (
                        replacement_target
                        if replacement_target and key == previous_target and channel.exact
                        else key
                    )
                    updated_channels[target] = replace(channel, name=target, tags=selected)
                else:
                    self.catalog.remove_channel(key)
            if replacement_target:
                self.catalog.rename_tag_channel(previous_target, replacement_target)
            else:
                self.catalog.remove_channel(previous_target)
            self.catalog.change_tag_metadata(name, replacement)
            self.catalog.write(changed(tags), updated_channels)
