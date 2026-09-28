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
from .catalog_store import ChannelCatalog
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
        return channels.catalog.read().all_tags(channels.registry.all_threads())


class ChannelManagement:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus):
        self.root = root
        self.registry = registry
        self.bus = bus
        self._wire_lock_path = root / "wire"
        self.catalog = ChannelCatalog(root / ChannelCatalog.filename)

    def channels(self) -> Sequence[str]:
        return list(self.catalog.read().views(self.registry.all_threads()))

    def _require_available_new_tags(self, proposed: frozenset[str]) -> None:
        document = self.catalog.read()
        threads = self.registry.all_threads()
        for tag in proposed - document.all_tags(threads):
            document.require_available_tag_name(tag, threads)

    def create_tag(self, name: str) -> Tag:
        tag = Tag(name)
        with _store_lock(self._wire_lock_path):
            threads = self.registry.all_threads()
            with self.catalog.editing() as document:
                document.create_tag(tag.name, threads)
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
                channels=self.catalog.read().targets_for(owner.tags),
            )

    def update_tags(
        self, name: str, *, add: frozenset[str] = frozenset(), remove: frozenset[str] = frozenset()
    ) -> Thread:
        for tag in add | remove:
            Tag(tag)
        with guard_legacy_root_write(self.root), _store_lock(self._wire_lock_path):
            self._require_available_new_tags(add)
            thread = self.registry.require(name)
            previous_channels = self.catalog.read().views(self.registry.all_threads())
            updated = replace(thread, tags=(thread.tags | add) - remove)
            self.registry.register(updated, self.registry.status(thread.name))
            updated = self.registry.require(thread.name)
            with self.catalog.editing() as document:
                document.remember_tags(add, time.time())
            if thread.role.executable and thread.tags != updated.tags:
                channels = {
                    **previous_channels,
                    **self.catalog.read().views(self.registry.all_threads()),
                }
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
        channel = Channel(name, tags)
        with _store_lock(self._wire_lock_path):
            threads = self.registry.all_threads()
            with self.catalog.editing() as document:
                document.set_channel(channel, threads)
                return document.resolve(channel.name)

    def set_saved_view(self, view: SavedView) -> SavedView:
        with _store_lock(self._wire_lock_path):
            threads = self.registry.all_threads()
            with self.catalog.editing() as document:
                document.set_view(view, threads)
        return view

    def delete_saved_view(self, name: str) -> None:
        with _store_lock(self._wire_lock_path), self.catalog.editing() as document:
            document.delete_view(name)

    def set_channel_metadata(self, name: str, *, parent: str | None, archived: bool) -> Channel:
        with _store_lock(self._wire_lock_path):
            threads = self.registry.all_threads()
            with self.catalog.editing() as document:
                return document.set_metadata(name, threads, parent=parent, archived=archived)

    def set_channel_any_mode(self, name: str, enabled: bool) -> Channel:
        with _store_lock(self._wire_lock_path):
            threads = self.registry.all_threads()
            with self.catalog.editing() as document:
                return document.set_any_mode(name, enabled, threads)

    def set_channel_sort(self, name: str, order: ThreadSort) -> Channel:
        with _store_lock(self._wire_lock_path):
            threads = self.registry.all_threads()
            with self.catalog.editing() as document:
                return document.set_preferences(name, threads, order=order)

    def set_channel_order(self, order: ChannelSort) -> ChannelSort:
        with _store_lock(self._wire_lock_path), self.catalog.editing() as document:
            document.list_order = order
        return order

    def set_channel_pinned(self, name: str, pinned: bool) -> Channel:
        with _store_lock(self._wire_lock_path):
            threads = self.registry.all_threads()
            with self.catalog.editing() as document:
                return document.set_preferences(name, threads, pinned=pinned)

    def set_thread_pinned(self, channel: str, name: str, pinned: bool) -> None:
        canonical_channel = channel if channel.startswith("#") else f"#{channel}"
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            snapshot = self.registry.snapshot()
            with self.catalog.editing() as document:
                view = document.views(snapshot.threads).get(canonical_channel)
                if view is None:
                    raise ValueError(f"Unknown channel: {canonical_channel!r}")
                if pinned and (
                    not view.matches(thread.tags)
                    or not thread.role.executable
                    or not snapshot.statuses[thread.name].visible
                ):
                    raise ValueError(
                        f"Thread {thread.name!r} is not a member of {canonical_channel}."
                    )
                document.set_thread_pinned(canonical_channel, thread.name, pinned)

    def delete_channel(self, name: str) -> None:
        canonical = name if name.startswith("#") else f"#{name}"
        with _store_lock(self._wire_lock_path), self.catalog.editing() as document:
            document.delete_channel(canonical)

    def rename_tag(self, name: str, new_name: str) -> None:
        Tag(new_name)
        if name == new_name:
            if name not in self.catalog.read().all_tags(self.registry.all_threads()):
                raise ValueError(f"Unknown tag: {name!r}")
            return
        self._change_tag(name, new_name)

    def delete_tag(self, name: str) -> None:
        self._change_tag(name, None)

    def _change_tag(self, name: str, replacement: str | None) -> None:
        Tag(name)
        with _store_lock(self._wire_lock_path):
            if name not in self.catalog.read().all_tags(self.registry.all_threads()):
                raise ValueError(f"Unknown tag: {name!r}")
            if replacement is None:
                self.catalog.read().require_unreferenced_tag(name)
            else:
                self.catalog.read().require_available_tag_name(
                    replacement, self.registry.all_threads(), previous=name
                )

            def changed(tags: frozenset[str]) -> frozenset[str]:
                return (tags - {name}) | ({replacement} if name in tags and replacement else set())

            for thread in self.registry.all_threads().values():
                if name in thread.tags:
                    self.registry.register(
                        replace(thread, tags=changed(thread.tags)),
                        self.registry.status(thread.name),
                    )
            with self.catalog.editing() as document:
                document.change_tag(name, replacement)
