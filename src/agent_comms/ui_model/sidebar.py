"""The comms sidebar as presentation models: channels and the threads they list.

Rows are derived from one CoordinationSnapshot. The view renders them and
updates only what a flush reports as changed; it does not re-derive comms
meaning (unread kinds, activity, labels) itself.
"""

from __future__ import annotations

from collections.abc import Callable
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace

from agent_comms.read_ledger import NoReadNotice, ReadNotice
from typing import NoReturn

from agent_comms.display_order import ChannelSort, NameChannelSort, ThreadSort
from agent_comms.presentation import CoordinationSnapshot, ThreadView
from agent_comms.thread_execution import ConversationPreparation, ThreadExecution
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.thread_model import ThreadModel
from agent_comms.thread_presentation import SessionDisconnected, ThreadPresentationStatus
from agent_comms.thread_status import ActiveThreadPresence
from agent_comms.ui_model.changes import KeyedModel
from agent_comms.ui_model.unread import UnreadPresentation, UnobservedUnread

from ..display_references import (
    ForeignReferences,
    PendingReferences,
    UnavailableReferences,
)


@dataclass(frozen=True)
class PersonUnread(ConversationPreparation):
    """Native people keep their saved-reply count even while stopped; external people use the bus."""

    person: ThreadView
    snapshot: CoordinationSnapshot

    def native(self) -> UnreadPresentation:
        return UnreadPresentation.for_thread(self.snapshot, self.person.thread.name)

    def external(self) -> UnreadPresentation:
        return UnreadPresentation.for_bus(self.snapshot, self.person.thread.name)


@dataclass(frozen=True)
class ThreadRowModel:
    incarnation: ThreadIncarnation
    title: str
    label: str
    summary: str
    status: ThreadPresentationStatus
    model: ThreadModel
    unread: UnreadPresentation
    active: bool
    execution: type[ThreadExecution]

    @property
    def name(self) -> str:
        return self.incarnation.name

    def disconnected(self, status: SessionDisconnected) -> ThreadRowModel:
        shown = status.presentation(self.title)
        return self.status.with_disconnection(
            self, lambda: replace(self, label=shown.label, summary=shown.summary, status=status))

    @classmethod
    def of(cls, person: ThreadView, unread: UnreadPresentation) -> ThreadRowModel:
        presentation = person.presentation
        return cls(
            person.thread.incarnation, presentation.title, presentation.label, presentation.summary,
            presentation.status, person.thread.model,
            unread,
            bool(person.status.items_with(ActiveThreadPresence, person)), person.thread.execution,
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
            tuple(name for name in view.members if name in snapshot.threads),
            frozenset(view.pinned_members),
            channel.order,
        )


class SidebarObservation(ABC):
    @abstractmethod
    def status_text(self) -> str: ...

    def shows_status(self) -> bool:
        return True

    def require_snapshot(self) -> CoordinationSnapshot:
        self.unavailable()

    def channel_order(self) -> type[ChannelSort]:
        return NameChannelSort

    def read_marker_notice(self) -> ReadNotice:
        return NoReadNotice()

    def channel_unread(self, target: str) -> UnreadPresentation:
        return UnobservedUnread()

    def bus_unread(self, target: str) -> UnreadPresentation:
        return UnobservedUnread()

    def unavailable(self) -> NoReturn:
        raise RuntimeError("Sidebar source has not been observed")

    @abstractmethod
    def references_for(self, root): ...


@dataclass(frozen=True)
class WaitingSidebar(SidebarObservation):
    def status_text(self) -> str:
        return "Reading comms"

    def references_for(self, root):
        return PendingReferences(root)


@dataclass(frozen=True)
class FailedSidebar(SidebarObservation):
    error: Exception

    def status_text(self) -> str:
        return str(self.error)

    def unavailable(self) -> NoReturn:
        raise self.error

    def references_for(self, root):
        return UnavailableReferences(root, str(self.error))


@dataclass(frozen=True)
class ObservedSidebar(SidebarObservation):
    snapshot: CoordinationSnapshot

    def status_text(self) -> str:
        return ""

    def shows_status(self) -> bool:
        return False

    def require_snapshot(self) -> CoordinationSnapshot:
        return self.snapshot

    def channel_order(self) -> type[ChannelSort]:
        return self.snapshot.channel_order

    def read_marker_notice(self) -> ReadNotice:
        return self.snapshot.read_marker_notice

    def channel_unread(self, target: str) -> UnreadPresentation:
        return UnreadPresentation.for_channel(self.snapshot, target)

    def bus_unread(self, target: str) -> UnreadPresentation:
        return UnreadPresentation.for_bus(self.snapshot, target)

    def references_for(self, root):
        references = self.snapshot.references
        return references if root == references.root else ForeignReferences(root, references.root)


class SidebarModel:
    """Channels and threads derived from one observed snapshot.

    ``derive`` inspects process identity off the UI thread; ``apply`` publishes
    the resulting rows and observation. Keyed models flush changes once per
    frame through the backend scheduler.
    """

    def __init__(self, schedule: Callable[[Callable[[], None]], None]):
        self.channels: KeyedModel[str, ChannelRowModel] = KeyedModel(schedule)
        self.threads: KeyedModel[str, ThreadRowModel] = KeyedModel(schedule)
        self.observation: SidebarObservation = WaitingSidebar()

    @staticmethod
    def derive(snapshot: CoordinationSnapshot) -> tuple[dict[str, ChannelRowModel], dict[str, ThreadRowModel]]:
        threads = {name: ThreadRowModel.of(
            person, person.thread.execution.prepare_conversation(PersonUnread(person, snapshot)))
            for name, person in snapshot.threads.items()}
        working = frozenset(name for name, row in threads.items() if row.status.working)
        channels = {name: ChannelRowModel.of(view, snapshot, working)
                    for name, view in snapshot.channels.items()}
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
