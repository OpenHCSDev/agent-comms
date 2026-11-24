"""Channel mutations and membership transactions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from agent_comms.display_order import ChannelSort, ThreadSort
from agent_comms.messaging.bus_publication import AgentOrigin
from agent_comms.messaging.channel_targets import ChannelTarget, CommandTarget, Tag
from agent_comms.messaging.channels import Channel, SavedView
from agent_comms.messaging.display_sources import ChannelOrderSource
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.messaging.messages import InfoKind, MembershipConversationRole, Message
from agent_comms.messaging.tag_edits import (
    KeepThreadsTagDisposition,
    TagChangeResult,
    TagChannelOwner,
    TagDisposition,
)
from agent_comms.registry.catalog_document import CatalogDocument
from agent_comms.registry.registration import Registration
from agent_comms.registry.relationship_catalog_source import RelationshipCatalogSource
from agent_comms.registry.tag_choice import ChangedTags, GivenTags, TagChoice
from agent_comms.registry.thread_management import ThreadManagement
from agent_comms.registry.threads import Thread


class ChannelManagement(
    TagChannelOwner,
    ChannelOrderSource[type[ThreadSort], type[ChannelSort]],
    RelationshipCatalogSource,
):
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
        self.catalog.change(lambda document: document.create_tag(tag.name))
        return tag

    def update_tags(
        self, name: str, *, add: frozenset[str] = frozenset(), remove: frozenset[str] = frozenset()
    ) -> Thread:
        for tag in add | remove:
            Tag.validate(tag)
        return self._update_tags(name, ChangedTags(add, remove))

    def replace_tags(self, name: str, tags: frozenset[str]) -> Thread:
        for tag in tags:
            Tag.validate(tag)
        return self._update_tags(name, GivenTags(tags))

    def _update_tags(self, name: str, choice: TagChoice) -> Thread:
        previous_channels = self.bus.channel_declarations()
        with self.registry.store.editing() as edit:
            thread = edit.document.require(name)

            def record(document: CatalogDocument) -> Thread:
                updated = replace(thread, tags=choice.for_child(thread.tags))
                for tag in updated.tags - document.tags:
                    document.require_available_tag_name(tag)
                self.registry._commit_registration(edit, edit.document.prepare_registration(
                    updated, edit.document.status(thread.name), new_owner=False))
                updated = edit.document.require(thread.name)
                document.remember_tags(updated.tags)
                return updated

            updated = self.catalog.change(record)
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
                            f"{announcer.name} {change.declared_name} {channel.name}",
                            InfoKind,
                            role=MembershipConversationRole(change),
                        ),
                        destination=CommandTarget.named(channel.name),
                        origin=AgentOrigin(),
                    )
        return updated

    def set_saved_view(self, view: SavedView) -> SavedView:
        with self.bus.opened_activity() as activity:
            return self.catalog.change(lambda document: document.set_view(view, activity))

    def delete_saved_view(self, name: str) -> None:
        self.catalog.change(lambda document: document.delete_view(name))

    def set_channel_metadata(self, name: str, *, parent: str | None, archived: bool) -> Channel:
        with self.bus.opened_activity() as activity:
            return self.catalog.change(lambda document: document.set_metadata(
                name, activity, parent=parent, archived=archived))

    def set_channel_archived(self, name: str, archived: bool) -> Channel:
        with self.bus.opened_activity() as activity:
            return self.catalog.change(lambda document: document.set_archived(name, archived, activity))

    def set_channel_any_mode(self, name: str, enabled: bool) -> Channel:
        with self.bus.opened_activity() as activity:
            return self.catalog.change(lambda document: document.set_any_mode(name, enabled, activity))

    def set_channel_sort(self, name: str, order: type[ThreadSort]) -> Channel:
        with self.bus.opened_activity() as activity:
            return self.catalog.change(lambda document: document.set_preferences(name, activity, order=order))

    def set_channel_order(self, order: type[ChannelSort]) -> type[ChannelSort]:
        return self.catalog.change(lambda document: document.set_list_order(order))

    def set_channel_pinned(self, name: str, pinned: bool) -> Channel:
        with self.bus.opened_activity() as activity:
            return self.catalog.change(lambda document: document.set_preferences(name, activity, pinned=pinned))

    def set_thread_pinned(self, channel: str, name: str, pinned: bool) -> None:
        with self.bus.opened_activity() as activity:
            thread = self.registry.require(name)
            snapshot = self.registry.snapshot()

            def record(document: CatalogDocument) -> None:
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

            self.catalog.change(record)

    def rename_tag(self, name: str, new_name: str) -> None:
        Tag.validate(new_name)
        if name == new_name:
            self.catalog.read().require_changeable_tag(name)
            return
        self._change_tag(name, new_name)

    def delete_tag(self, name: str, *, disposition: TagDisposition = KeepThreadsTagDisposition(),
                   confirmed: bool = False) -> TagChangeResult:
        Tag.validate(name)
        disposition.require_confirmed((name,), confirmed)
        snapshot = self.registry.snapshot()
        self.catalog.read().require_changeable_tag(name)
        cohort = tuple(thread for thread in snapshot.threads.values() if name in thread.tags)
        return disposition.apply(self, name, cohort)

    def _change_tag(self, name: str, replacement: str | None) -> None:
        Tag.validate(name)
        self.catalog.read().require_changeable_tag(name)
        if replacement is not None:
            self.catalog.read().require_available_rename(name, replacement)

        cohort = tuple(thread for thread in self.registry.all_threads().values() if name in thread.tags)
        self._apply_tag_change(name, replacement, cohort)

    def _apply_tag_change(self, name: str, replacement: str | None,
                             cohort: tuple[Thread, ...]) -> None:
        with self.registry.store.editing() as edit:
            def record(document: CatalogDocument) -> None:
                document.change_tag(name, replacement)
                edit.document.change_tag(cohort, name, replacement)
                edit.commit()
            self.catalog.change(record)
