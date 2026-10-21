"""Channel mutations and membership transactions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar
from enum import Enum
from pathlib import Path

from .active_route import guard_original_root_write
from .bus_publication import AgentOrigin
from .channel_targets import ChannelTarget, Tag
from .channels import Channel, SavedView
from .cli_result import CliResult
from .display_order import ChannelSort, ThreadSort
from .message_bus import MessageBus
from .messages import InfoKind, Message, MembershipConversationRole
from .registration import Registration
from .threads import Thread
from .thread_management import ChangedTags, GivenTags
from .thread_identity import ThreadIncarnation
from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .thread_management import TagChoice, ThreadManagement


@dataclass(frozen=True)
class TagChangeResult(CliResult):
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
    def confirmation(self, tag: str | tuple[str, ...]) -> str: ...

    @staticmethod
    def tag_names(tag: str | tuple[str, ...]) -> str:
        return ', '.join(f'#{name}' for name in ((tag,) if isinstance(tag, str) else tag))

    @abstractmethod
    def apply(self, channels: ChannelManagement, tag: str, cohort: tuple[Thread, ...]) -> TagChangeResult: ...


class KeepThreadsTagDisposition(TagDisposition):
    label = "Remove tag; keep threads"
    requires_confirmation = False

    def confirmation(self, tag: str | tuple[str, ...]) -> str:
        return f"Remove {self.tag_names(tag)} from their threads? Threads, saved views and history remain."

    def apply(self, channels, tag, cohort):
        channels._apply_tag_change(tag, None, cohort)
        return TagChangeResult(tag, True)


class ArchiveThreadsTagDisposition(TagDisposition):
    label = "Archive tagged threads; keep tag"

    def confirmation(self, tag: str | tuple[str, ...]) -> str:
        return f"Archive all stopped threads tagged with {self.tag_names(tag)}? Tags and history remain; active owners are refused."

    def apply(self, channels, tag, cohort):
        channels.threads._archive_unlocked(cohort)
        return TagChangeResult(tag, False, archived_threads=tuple(thread.incarnation for thread in cohort))


class DeleteThreadsTagDisposition(TagDisposition):
    label = "Delete tagged threads and remove tag"

    def confirmation(self, tag: str | tuple[str, ...]) -> str:
        return f"Delete ALL inactive threads tagged with {self.tag_names(tag)} and close all their views? Active owners are refused; retained history and uncertain inputs are preserved."

    def deletion_cohort(self, channels, tag, cohort) -> tuple[Thread, ...]:
        return cohort

    def apply(self, channels, tag, cohort):
        selected = self.deletion_cohort(channels, tag, cohort)
        channels.threads._delete_unlocked(selected)
        channels._apply_tag_change(tag, None, tuple(thread for thread in cohort
                                                     if thread not in selected))
        return TagChangeResult(tag, True, tuple(thread.incarnation for thread in selected))


class DeleteExclusiveInactiveThreadsTagDisposition(DeleteThreadsTagDisposition):
    label = "Delete inactive single-tag threads; remove tag from others"

    def confirmation(self, tag: str | tuple[str, ...]) -> str:
        return (f"For {self.tag_names(tag)}, delete only inactive threads with that sole tag, "
                "and remove each selected tag from the remaining threads? Active owners, multitag threads, their other "
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
        return channels.catalog.read().tags


class ChannelManagement:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, threads: ThreadManagement):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.threads = threads
        self.catalog = threads.catalog

    def channels(self) -> Sequence[str]:
        return list(self.bus.channel_declarations())

    def create_tag(self, name: str) -> Tag:
        tag = Tag(name)
        with self.catalog.editing() as document:
            document.create_tag(tag.name)
        return tag

    def update_tags(
        self, name: str, *, add: frozenset[str] = frozenset(), remove: frozenset[str] = frozenset()
    ) -> Thread:
        for tag in add | remove:
            Tag.validate(tag)
        with guard_original_root_write(self.root):
            return self._update_tags(name, ChangedTags(add, remove))

    def replace_tags(self, name: str, tags: frozenset[str]) -> Thread:
        for tag in tags:
            Tag.validate(tag)
        with guard_original_root_write(self.root):
            return self._update_tags(name, GivenTags(tags))

    def _update_tags(self, name: str, choice: TagChoice) -> Thread:
        previous_channels = self.bus.channel_declarations()
        with self.registry.store.locked(), self.catalog.editing() as document:
            thread = self.registry.require(name)
            updated = replace(thread, tags=choice.for_child(thread.tags))
            for tag in updated.tags - document.tags:
                document.require_available_tag_name(tag)
            self.registry.register(updated, self.registry.status(thread.name))
            updated = self.registry.require(thread.name)
            document.remember_tags(updated.tags)
        announcers = thread.role.executor(thread) if thread.tags != updated.tags else ()
        for announcer in announcers:
            channels = {
                **previous_channels,
                **self.bus.channel_declarations(),
            }
            for channel in channels.values():
                for change in channel.membership_changes(thread.tags, updated.tags):
                    self.bus.publisher.publish_ordinary(
                        Message(
                            announcer.name,
                            channel.name,
                            f"{announcer.name} {change.value} {channel.name}",
                            InfoKind,
                            role=MembershipConversationRole(change),
                        ),
                        origin=AgentOrigin(),
                    )
        return updated

    def set_saved_view(self, view: SavedView) -> SavedView:
        with self.bus.opened_activity() as activity, self.catalog.editing() as document:
            document.set_view(view, activity)
            result = document.saved_views[view.name]
        return result

    def delete_saved_view(self, name: str) -> None:
        with self.catalog.editing() as document:
            document.delete_view(name)

    def set_channel_metadata(self, name: str, *, parent: str | None, archived: bool) -> Channel:
        with self.bus.opened_activity() as activity, self.catalog.editing() as document:
            return document.set_metadata(name, activity, parent=parent, archived=archived)

    def set_channel_archived(self, name: str, archived: bool) -> Channel:
        with self.bus.opened_activity() as activity, guard_original_root_write(self.root), self.catalog.editing() as document:
            channel = document.require_channel(name, activity)
            return document.set_metadata(channel.name, activity, parent=channel.parent, archived=archived)

    def set_channel_any_mode(self, name: str, enabled: bool) -> Channel:
        with self.bus.opened_activity() as activity, self.catalog.editing() as document:
            return document.set_any_mode(name, enabled, activity)

    def set_channel_sort(self, name: str, order: type[ThreadSort]) -> Channel:
        with self.bus.opened_activity() as activity, self.catalog.editing() as document:
            return document.set_preferences(name, activity, order=order)

    def set_channel_order(self, order: type[ChannelSort]) -> type[ChannelSort]:
        with self.catalog.editing() as document:
            document.list_order = order
        return order

    def set_channel_pinned(self, name: str, pinned: bool) -> Channel:
        with self.bus.opened_activity() as activity, self.catalog.editing() as document:
            return document.set_preferences(name, activity, pinned=pinned)

    def set_thread_pinned(self, channel: str, name: str, pinned: bool) -> None:
        with self.bus.opened_activity() as activity:
            thread = self.registry.require(name)
            snapshot = self.registry.snapshot()
            with self.catalog.editing() as document:
                view = document.require_channel(channel, activity)
                if pinned:
                    thread.role.require_executable(ValueError(
                        f"Thread {thread.name!r} is not a member of {view.name}."
                    ))
                if pinned:
                    message = f"Thread {thread.name!r} is not a member of {view.name}."
                    if not view.matches(thread.tags):
                        raise ValueError(message)
                    snapshot.status(thread.name).require_visible(message)
                document.set_thread_pinned(view.name, thread.name, pinned)

    def rename_tag(self, name: str, new_name: str) -> None:
        Tag.validate(new_name)
        if name == new_name:
            if name not in self.catalog.read().tags:
                raise ValueError(f"Unknown tag: {name!r}")
            return
        self._change_tag(name, new_name)

    def delete_tag(self, name: str, *, disposition: TagDisposition = KeepThreadsTagDisposition(),
                   confirmed: bool = False) -> TagChangeResult:
        Tag.validate(name)
        if disposition.requires_confirmation and not confirmed:
            raise ValueError(disposition.confirmation(name))
        with guard_original_root_write(self.root):
            snapshot = self.registry.snapshot()
            if name not in self.catalog.read().tags:
                raise ValueError(f"Unknown tag: {name!r}")
            cohort = tuple(thread for thread in snapshot.threads.values() if name in thread.tags)
            return disposition.apply(self, name, cohort)

    def _change_tag(self, name: str, replacement: str | None) -> None:
        Tag.validate(name)
        with guard_original_root_write(self.root):
            if name not in self.catalog.read().tags:
                raise ValueError(f"Unknown tag: {name!r}")
            if replacement is not None:
                self.catalog.read().require_available_tag_name(
                    replacement, previous=name
                )

            cohort = tuple(thread for thread in self.registry.all_threads().values() if name in thread.tags)
            self._apply_tag_change(name, replacement, cohort)

    def _apply_tag_change(self, name: str, replacement: str | None,
                             cohort: tuple[Thread, ...]) -> None:
        with self.catalog.editing() as document:
            document.change_tag(name, replacement)
            self.registry.change_tag(cohort, name, replacement)
