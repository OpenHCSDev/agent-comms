"""The comms sidebar as presentation models: channels and the threads they list.

Rows are derived from one CoordinationSnapshot. The view renders them and
updates only what a flush reports as changed; it does not re-derive comms
meaning (unread kinds, activity, labels) itself.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import NoReturn

from agent_comms.display_order import ChannelSort, NameChannelSort, ThreadSort
from agent_comms.messaging.display_references import (
    PendingReferences,
    RetiredThreadReference,
    ThreadDisplayReference,
    ThreadNames,
    ThreadReference,
    ThreadReferenceReader,
    UnavailableReferences,
)
from agent_comms.messaging.read_ledger import NoReadNotice, ReadNotice
from agent_comms.presentation import CoordinationSnapshot, ThreadView
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.thread_model import ThreadModel
from agent_comms.registry.thread_presentation import SessionDisconnected, ThreadPresentationStatus
from agent_comms.registry.thread_status import ActiveThreadPresence
from agent_comms.registry.threads import ConversationPreparation
from agent_comms.ui_model.changes import KeyedModel
from agent_comms.ui_model.unread import UnobservedUnread, UnreadPresentation


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
            bool(person.status.items_with(ActiveThreadPresence, person)),
        )


@dataclass(frozen=True)
class ChannelRowModel:
    """One channel's row. It holds the channel's agent counts and tags, and spells them
    for each place they are shown; a row that pre-baked its label had the counts in a
    string the second line could not get at, and its tags only in a tooltip."""

    name: str
    pinned: bool
    active_agents: int
    registered_agents: int
    tags: tuple[str, ...]
    unread: UnreadPresentation
    working_agents: int
    members: tuple[str, ...]
    pinned_members: frozenset[str]
    order: type[ThreadSort]

    @property
    def active(self) -> bool:
        return bool(self.working_agents)

    def label(self) -> str:
        """The row's first line: the channel's name, pinned or not."""
        return f"{'* ' if self.pinned else ''}{self.name}"

    def summary(self) -> str:
        """In-turn / online / total counts, followed by the channel's tags."""
        return (f"{self.working_agents}/{self.active_agents}/{self.registered_agents} agents"
                f" · tags: {self.tag_list()}")

    def tooltip(self) -> str:
        """The same facts in full words, for a reader who stopped to hover."""
        return (f"{self.working_agents} in turn / {self.active_agents} online"
                f" / {self.registered_agents} total · tags: {self.tag_list()}")

    def tag_list(self) -> str:
        return ", ".join(self.tags) or "all"

    @classmethod
    def of(cls, view, snapshot: CoordinationSnapshot, working: frozenset[str]) -> ChannelRowModel:
        channel = view.channel
        return cls(
            channel.name,
            channel.pinned,
            view.active_agents,
            view.registered_agents,
            tuple(sorted(channel.tags)),
            UnreadPresentation.for_channel(snapshot, channel.name),
            sum(name in working for name in view.members),
            tuple(name for name in view.members if name in snapshot.threads),
            frozenset(view.pinned_members),
            channel.order,
        )


@dataclass(frozen=True)
class PeerUnread(ThreadReferenceReader[UnreadPresentation]):
    """A DM peer's unread count, as one sidebar observation answers it."""

    observation: SidebarObservation

    def current(self, reference: ThreadDisplayReference) -> UnreadPresentation:
        return self.observation.bus_unread(reference.name)

    def retired(self, reference: RetiredThreadReference) -> UnreadPresentation:
        """A retired birth's tab can paint once before retirement closes it: nothing unread."""
        return UnobservedUnread()


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

    def peer_unread(self, peer: ThreadIncarnation) -> UnreadPresentation:
        """A DM peer's unread, named by its birth: before a read there is no count to show."""
        return UnobservedUnread()

    def unavailable(self) -> NoReturn:
        raise RuntimeError("Sidebar source has not been observed")

    def thread_names(self) -> ThreadNames:
        """Names only a read answers; a destination that needs none never asks."""
        return UnobservedThreadNames(self)

    @abstractmethod
    def references_for(self, root): ...


@dataclass(frozen=True)
class UnobservedThreadNames(ThreadNames):
    """Before the sidebar is read, a thread's current name is this observation's refusal."""

    observation: SidebarObservation

    def thread(self, incarnation: ThreadIncarnation) -> ThreadReference:
        self.observation.unavailable()


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

    def thread_names(self) -> ThreadNames:
        return self.snapshot.references

    def channel_order(self) -> type[ChannelSort]:
        return self.snapshot.channel_order

    def read_marker_notice(self) -> ReadNotice:
        return self.snapshot.read_marker_notice

    def channel_unread(self, target: str) -> UnreadPresentation:
        return UnreadPresentation.for_channel(self.snapshot, target)

    def bus_unread(self, target: str) -> UnreadPresentation:
        return UnreadPresentation.for_bus(self.snapshot, target)

    def peer_unread(self, peer: ThreadIncarnation) -> UnreadPresentation:
        """The peer's current name and its unread count come from this one snapshot. A name the
        caller resolved from an earlier cut can be one a rename has since replaced, and a
        retired birth answers for itself."""
        return self.snapshot.references.thread(peer).read(PeerUnread(self))

    def references_for(self, root):
        return self.snapshot.references.at(root)


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
            person, person.thread.prepare_conversation(PersonUnread(person, snapshot)))
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
