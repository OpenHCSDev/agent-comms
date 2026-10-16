"""The comms sidebar as presentation models: channels and the threads they list.

Rows are derived from one CoordinationSnapshot. The view renders them and
updates only what a flush reports as changed; it does not re-derive comms
meaning (unread kinds, activity, labels) itself.
"""

from __future__ import annotations

from collections.abc import Callable
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import NoReturn

from agent_comms.display_order import ChannelSort, NameChannelSort, ThreadSort
from agent_comms.presentation import CoordinationSnapshot, ThreadView
from agent_comms.thread_execution import ConversationPreparation, ThreadExecution
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.thread_presentation import ThreadPresentationStatus
from agent_comms.ui_model.changes import KeyedModel
from agent_comms.ui_model.unread import UnreadPresentation, UnobservedUnread


@dataclass(frozen=True)
class PersonUnread(ConversationPreparation):
    """Which unread count a person shows: its own transcript while it runs natively, else the bus."""

    person: ThreadView
    snapshot: CoordinationSnapshot

    def native(self) -> UnreadPresentation:
        name = self.person.thread.name
        if self.person.status.active:
            return UnreadPresentation.for_thread(self.snapshot, name)
        return UnreadPresentation.for_bus(self.snapshot, name)

    def external(self) -> UnreadPresentation:
        return UnreadPresentation.for_bus(self.snapshot, self.person.thread.name)


@dataclass(frozen=True)
class ThreadRowModel:
    incarnation: ThreadIncarnation
    title: str
    label: str
    summary: str
    status: ThreadPresentationStatus
    model: str | None
    unread: UnreadPresentation
    active: bool
    execution: type[ThreadExecution]

    @property
    def name(self) -> str:
        return self.incarnation.name

    @classmethod
    def of(cls, person: ThreadView, snapshot: CoordinationSnapshot | None) -> ThreadRowModel:
        presentation = person.presentation
        return cls(
            person.thread.incarnation, presentation.title, presentation.label, presentation.summary,
            presentation.status, person.thread.model,
            (UnobservedUnread() if snapshot is None else
             person.thread.execution.prepare_conversation(PersonUnread(person, snapshot))),
            person.status.active, person.thread.execution,
        )


@dataclass(frozen=True)
class ChannelRowModel:
    name: str
    label: str
    tooltip: str
    unread: UnreadPresentation
    active: bool
    members: tuple[str, ...]
    pinned_members: frozenset[str]
    order: type[ThreadSort]

    @classmethod
    def of(cls, view, snapshot: CoordinationSnapshot, working: frozenset[str]) -> ChannelRowModel:
        channel = view.channel
        return cls(
            channel.name,
            f"{'* ' if channel.pinned else ''}{channel.name} {view.active_agents}/{view.registered_agents}",
            f"{view.active_agents} running or idle / {view.registered_agents} registered agents"
            f" · tags: {', '.join(sorted(channel.tags)) or 'all'}",
            UnreadPresentation.for_channel(snapshot, channel.name),
            any(name in working for name in view.members),
            tuple(name for name in view.members if name in {person.thread.name for person in snapshot.threads}),
            frozenset(view.pinned_members),
            channel.order,
        )


class SidebarObservation(ABC):
    snapshot: CoordinationSnapshot | None = None

    def require_snapshot(self) -> CoordinationSnapshot:
        snapshot = self.snapshot
        if snapshot is None:
            self.unavailable()
        return snapshot

    def unavailable(self) -> NoReturn:
        raise RuntimeError("Sidebar source has not been observed")

    @abstractmethod
    def references_for(self, root): ...


@dataclass(frozen=True)
class WaitingSidebar(SidebarObservation):
    def references_for(self, root):
        from ..display_references import PendingReferences
        return PendingReferences(root)


@dataclass(frozen=True)
class FailedSidebar(SidebarObservation):
    error: Exception

    def unavailable(self) -> NoReturn:
        raise self.error

    def references_for(self, root):
        from ..display_references import UnavailableReferences
        return UnavailableReferences(root, str(self.error))


@dataclass(frozen=True)
class ObservedSidebar(SidebarObservation):
    snapshot: CoordinationSnapshot = field()

    def references_for(self, root):
        from ..display_references import ForeignReferences
        references = self.snapshot.references
        return references if root == references.root else ForeignReferences(root, references.root)


class SidebarModel:
    """Channels in display order and the threads they list, from one snapshot.

    ``derive`` builds every row (it may inspect process identity, so call it
    off the UI thread); ``apply`` publishes them with the snapshot they came
    from, which views borrow for per-target unread counts and membership.
    Each keyed model flushes its change set once per frame through the
    backend's scheduler.
    """

    def __init__(self, schedule: Callable[[Callable[[], None]], None]):
        self.channels: KeyedModel[str, ChannelRowModel] = KeyedModel(schedule)
        self.threads: KeyedModel[str, ThreadRowModel] = KeyedModel(schedule)
        self.observation: SidebarObservation = WaitingSidebar()

    @property
    def snapshot(self) -> CoordinationSnapshot | None:
        return self.observation.snapshot

    def references_for(self, root):
        return self.observation.references_for(root)

    @property
    def channel_order(self) -> type[ChannelSort]:
        return self.snapshot.channel_order if self.snapshot is not None else NameChannelSort

    @property
    def read_marker_notice(self) -> str | None:
        return self.snapshot.read_marker_notice if self.snapshot is not None else None

    @property
    def filters(self) -> tuple[bool, bool] | None:
        """The stopped/archived filters the published rows were read with."""
        if self.snapshot is None:
            return None
        return self.snapshot.show_stopped, self.snapshot.show_archived

    @staticmethod
    def derive(snapshot: CoordinationSnapshot) -> tuple[dict[str, ChannelRowModel], dict[str, ThreadRowModel]]:
        threads = {person.thread.name: ThreadRowModel.of(person, snapshot) for person in snapshot.threads}
        working = frozenset(member for name, row in threads.items()
                            for member in row.status.working_members(name))
        channels = {view.channel.name: ChannelRowModel.of(view, snapshot, working)
                    for view in snapshot.channels}
        return channels, threads

    def clear(self, error: Exception) -> None:
        """Retain why the source is unreadable, not a successful empty catalog."""
        self.observation = FailedSidebar(error)
        self.threads.replace({})
        self.channels.replace({})

    def apply(self, snapshot: CoordinationSnapshot,
              derived: tuple[dict[str, ChannelRowModel], dict[str, ThreadRowModel]]) -> None:
        """Publish rows derived from ``snapshot`` (on the UI thread; derivation is not)."""
        channels, threads = derived
        self.observation = ObservedSidebar(snapshot)
        self.threads.replace(threads)
        self.channels.replace(channels)
