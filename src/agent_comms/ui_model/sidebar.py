"""The comms sidebar as presentation models: channels and the threads they list.

Rows are derived from one CoordinationSnapshot. The view renders them and
updates only what a flush reports as changed; it does not re-derive comms
meaning (unread kinds, activity, labels) itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agent_comms.display_order import ChannelSort, ThreadSort
from agent_comms.presentation import CoordinationSnapshot, ThreadView
from agent_comms.thread_execution import ConversationPreparation
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.ui_model.changes import KeyedModel
from agent_comms.ui_model.unread import ExactUnread, UnreadPresentation


@dataclass(frozen=True)
class PersonUnread(ConversationPreparation):
    """Which unread count a person shows: its own transcript while it runs natively, else the bus."""

    person: ThreadView
    snapshot: CoordinationSnapshot

    def native(self) -> UnreadPresentation:
        name = self.person.thread.name
        if self.person.status.active:
            return UnreadPresentation.for_thread(self.snapshot, name)
        return ExactUnread(self.snapshot.unread.get(name, 0))

    def external(self) -> UnreadPresentation:
        return ExactUnread(self.snapshot.unread.get(self.person.thread.name, 0))


@dataclass(frozen=True)
class ThreadRowModel:
    incarnation: ThreadIncarnation
    title: str
    label: str
    summary: str
    busy: bool
    model: str | None
    unread: UnreadPresentation
    active: bool

    @property
    def name(self) -> str:
        return self.incarnation.name

    @classmethod
    def of(cls, person: ThreadView, snapshot: CoordinationSnapshot) -> ThreadRowModel:
        presentation = person.presentation
        return cls(
            person.thread.incarnation, presentation.title, presentation.label, presentation.summary,
            presentation.busy, person.runtime.model if person.runtime else person.thread.model,
            person.thread.execution.prepare_conversation(PersonUnread(person, snapshot)),
            person.status.active,
        )


@dataclass(frozen=True)
class ChannelRowModel:
    name: str
    label: str
    tooltip: str
    unread: int
    active: bool
    members: tuple[str, ...]
    pinned_members: frozenset[str]
    order: ThreadSort

    @classmethod
    def of(cls, view, snapshot: CoordinationSnapshot, busy: frozenset[str]) -> ChannelRowModel:
        channel = view.channel
        return cls(
            channel.name,
            f"{'* ' if channel.pinned else ''}{channel.name} {view.active_agents}/{view.registered_agents}",
            f"{view.active_agents} running or idle / {view.registered_agents} registered agents"
            f" · tags: {', '.join(sorted(channel.tags)) or 'all'}",
            snapshot.channel_unread.get(channel.name, 0),
            any(name in busy for name in view.members),
            tuple(name for name in view.members if name in {person.thread.name for person in snapshot.threads}),
            frozenset(view.pinned_members),
            channel.order,
        )


class SidebarModel:
    """Channels in display order and the threads they list, from one snapshot.

    ``apply`` derives every row (it may inspect process identity, so call it
    off the UI thread) and replaces the models; each model flushes its change
    set once per frame through the backend's scheduler.
    """

    def __init__(self, schedule: Callable[[Callable[[], None]], None]):
        self.channels: KeyedModel[str, ChannelRowModel] = KeyedModel(schedule)
        self.threads: KeyedModel[str, ThreadRowModel] = KeyedModel(schedule)
        self.channel_order: ChannelSort = ChannelSort.NAME
        self.read_marker_notice: str | None = None
        self.filters: tuple[bool, bool] | None = None

    @staticmethod
    def derive(snapshot: CoordinationSnapshot) -> tuple[dict[str, ChannelRowModel], dict[str, ThreadRowModel]]:
        threads = {person.thread.name: ThreadRowModel.of(person, snapshot) for person in snapshot.threads}
        busy = frozenset(name for name, row in threads.items() if row.busy)
        channels = {view.channel.name: ChannelRowModel.of(view, snapshot, busy) for view in snapshot.channels}
        return channels, threads

    def apply(self, snapshot: CoordinationSnapshot,
              derived: tuple[dict[str, ChannelRowModel], dict[str, ThreadRowModel]]) -> None:
        """Publish rows derived from ``snapshot`` (on the UI thread; derivation is not)."""
        channels, threads = derived
        self.channel_order = snapshot.channel_order
        self.read_marker_notice = snapshot.read_marker_notice
        self.filters = (snapshot.show_stopped, snapshot.show_archived)
        self.threads.replace(threads)
        self.channels.replace(channels)
