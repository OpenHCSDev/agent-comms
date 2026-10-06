"""Channel mutations and membership transactions."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar
from enum import Enum
from pathlib import Path

from .active_route import guard_original_root_write
from .channel_targets import Tag
from .channels import Channel, SavedView
from .display_order import ChannelSort, ThreadSort
from .message_bus import MessageBus
from .messages import MembershipChange, Message, MessageType
from .registration import Registration
from .store_files import _store_lock
from .threads import Thread
from .thread_identity import ThreadIncarnation
from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .thread_management import ThreadManagement


@dataclass(frozen=True)
class TagChangeResult:
    tag: str
    removed_tag: bool
    removed_threads: tuple[ThreadIncarnation, ...] = ()
    archived_threads: tuple[ThreadIncarnation, ...] = ()


@dataclass(frozen=True)
class TagDisposition(DeclaredFamily, affix="TagDisposition"):
    """Each declared channel operation owns its membership effect and warning."""
    label: ClassVar[str]
    requires_confirmation: ClassVar[bool] = True

    @abstractmethod
    def confirmation(self, tag: str) -> str: ...

    @abstractmethod
    def apply(self, channels: ChannelManagement, tag: str, cohort: tuple[Thread, ...]) -> TagChangeResult: ...


class KeepThreadsTagDisposition(TagDisposition):
    label = "Remove tag; keep threads"
    requires_confirmation = False

    def confirmation(self, tag: str) -> str:
        return f"Remove #{tag} from its threads? Threads, saved views and history remain."

    def apply(self, channels, tag, cohort):
        channels._change_tag_unlocked(tag, None, cohort)
        return TagChangeResult(tag, True)


class ArchiveThreadsTagDisposition(TagDisposition):
    label = "Archive tagged threads; keep tag"

    def confirmation(self, tag: str) -> str:
        return f"Archive all stopped threads tagged #{tag}? Tags and history remain; active owners are refused."

    def apply(self, channels, tag, cohort):
        channels.threads._archive_unlocked(cohort)
        return TagChangeResult(tag, False, archived_threads=tuple(thread.incarnation for thread in cohort))


class DeleteThreadsTagDisposition(TagDisposition):
    label = "Delete tagged threads and remove tag"

    def confirmation(self, tag: str) -> str:
        return f"Delete ALL inactive threads tagged #{tag} and close all their views? Active owners are refused; retained history and uncertain inputs are preserved."

    def deletion_cohort(self, channels, tag, cohort) -> tuple[Thread, ...]:
        return cohort

    def apply(self, channels, tag, cohort):
        selected = self.deletion_cohort(channels, tag, cohort)
        channels.threads._delete_unlocked(selected)
        channels._change_tag_unlocked(tag, None, tuple(thread for thread in cohort
                                                     if thread not in selected))
        return TagChangeResult(tag, True, tuple(thread.incarnation for thread in selected))


class DeleteExclusiveInactiveThreadsTagDisposition(DeleteThreadsTagDisposition):
    label = "Delete inactive single-tag threads; remove tag from others"

    def confirmation(self, tag: str) -> str:
        return (f"Delete only inactive threads whose sole tag is #{tag}, and remove #{tag} "
                "from the remaining threads? Active owners, multitag threads, their other "
                "tags, retained history and uncertain inputs remain.")

    def deletion_cohort(self, channels, tag, cohort):
        return tuple(thread for thread in cohort if thread.tags == frozenset({tag})
                     and not channels.registry.status(thread.name).active
                     and not thread.process_alive)


class TagAction(Enum):
    LIST = "list"
    CREATE = "create"
    RENAME = "rename"
    DELETE = "delete"

    def apply(
        self, channels: ChannelManagement, name: str = "", new_name: str = "", *,
        disposition: TagDisposition = KeepThreadsTagDisposition(), confirmed: bool = False
    ) -> frozenset[str]:
        match self:
            case self.CREATE:
                channels.create_tag(name)
            case self.RENAME:
                channels.rename_tag(name, new_name)
            case self.DELETE:
                channels.delete_tag(name, disposition=disposition, confirmed=confirmed)
        return channels.catalog.read().all_tags(channels.registry.all_threads())


class ChannelManagement:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, threads: ThreadManagement):
        self.root = root
        self.registry = registry
        self.bus = bus
        self._wire_lock_path = root / "wire"
        self.threads = threads
        self.catalog = threads.catalog

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

    def update_tags(
        self, name: str, *, add: frozenset[str] = frozenset(), remove: frozenset[str] = frozenset()
    ) -> Thread:
        for tag in add | remove:
            Tag(tag)
        with guard_original_root_write(self.root), _store_lock(self._wire_lock_path):
            return self._update_tags_locked(name, add=add, remove=remove)

    def replace_tags(self, name: str, tags: frozenset[str]) -> Thread:
        for tag in tags:
            Tag(tag)
        with guard_original_root_write(self.root), _store_lock(self._wire_lock_path):
            thread = self.registry.require(name)
            return self._update_tags_locked(thread.name, add=tags - thread.tags,
                                            remove=thread.tags - tags)

    def _update_tags_locked(self, name, *, add, remove) -> Thread:
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
                if channel.view is not None:
                    continue
                before, after = channel.matches(thread.tags), channel.matches(updated.tags)
                if before != after:
                    change = MembershipChange.JOINED if after else MembershipChange.LEFT
                    self.bus.publisher.publish_ordinary(
                        Message(
                            thread.name,
                            channel.name,
                            f"{thread.name} {change.value} {channel.name}",
                            MessageType.INFO,
                            membership=change,
                        )
                    )
        return updated

    def set_saved_view(self, view: SavedView) -> SavedView:
        with _store_lock(self._wire_lock_path):
            threads = self.registry.all_threads()
            with self.catalog.editing() as document:
                document.set_view(view, threads)
                result = document.saved_views[view.name]
        return result

    def delete_saved_view(self, name: str) -> None:
        with _store_lock(self._wire_lock_path), self.catalog.editing() as document:
            document.delete_view(name)

    def set_channel_metadata(self, name: str, *, parent: str | None, archived: bool) -> Channel:
        with _store_lock(self._wire_lock_path):
            threads = self.registry.all_threads()
            with self.catalog.editing() as document:
                return document.set_metadata(name, threads, parent=parent, archived=archived)

    def set_channel_archived(self, name: str, archived: bool) -> Channel:
        with guard_original_root_write(self.root), _store_lock(self._wire_lock_path):
            threads = self.registry.all_threads()
            with self.catalog.editing() as document:
                target = name if name.startswith('#') else f'#{name}'
                channel = document.views(threads).get(target)
                if channel is None:
                    raise ValueError(f'Unknown channel: {target!r}')
                return document.set_metadata(target, threads, parent=channel.parent, archived=archived)

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

    def rename_tag(self, name: str, new_name: str) -> None:
        Tag(new_name)
        if name == new_name:
            if name not in self.catalog.read().all_tags(self.registry.all_threads()):
                raise ValueError(f"Unknown tag: {name!r}")
            return
        self._change_tag(name, new_name)

    def delete_tag(self, name: str, *, disposition: TagDisposition = KeepThreadsTagDisposition(),
                   confirmed: bool = False) -> TagChangeResult:
        Tag(name)
        if disposition.requires_confirmation and not confirmed:
            raise ValueError(disposition.confirmation(name))
        with guard_original_root_write(self.root), _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            if name not in self.catalog.read().all_tags(snapshot.threads):
                raise ValueError(f"Unknown tag: {name!r}")
            cohort = tuple(thread for thread in snapshot.threads.values() if name in thread.tags)
            return disposition.apply(self, name, cohort)

    def _change_tag(self, name: str, replacement: str | None) -> None:
        Tag(name)
        with guard_original_root_write(self.root), _store_lock(self._wire_lock_path):
            if name not in self.catalog.read().all_tags(self.registry.all_threads()):
                raise ValueError(f"Unknown tag: {name!r}")
            if replacement is not None:
                self.catalog.read().require_available_tag_name(
                    replacement, self.registry.all_threads(), previous=name
                )

            cohort = tuple(thread for thread in self.registry.all_threads().values() if name in thread.tags)
            self._change_tag_unlocked(name, replacement, cohort)

    def _change_tag_unlocked(self, name: str, replacement: str | None,
                             cohort: tuple[Thread, ...]) -> None:
        for thread in cohort:
            tags = (thread.tags - {name}) | ({replacement} if replacement else set())
            self.registry.register(replace(thread, tags=tags), self.registry.status(thread.name))
        with self.catalog.editing() as document:
            document.change_tag(name, replacement)
