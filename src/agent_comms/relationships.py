"""Explicit work relationships and a bounded, read-only thread sidebar projection.

Collaboration is cooperative metadata, not a dispatch or delivery instruction.
The recent wire window follows the core's current delivery scope; it does not
claim historical proof that a model consumed an old channel message.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from functools import cmp_to_key
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from agent_comms.field_codec import GoalId
from agent_comms import field_codec

from . import presentation
from .channel_targets import CommandTarget, NamedTarget
from .command import Command
from .declared_family import DeclaredFamily
from .display_order import CreatedThreadSort, LastActivityThreadSort, ThreadSort
from .errors import RelationViolationError, UnregisteredThreadError
from .goals import Goal, GoalMentionBinding
from .locked_store import DocumentChange
from .maintenance_barrier import CarriedDocument
from .messages import MessageInput
from .peer_relevance import (
    ChannelmateRelevance,
    CollaboratorRelevance,
    GoalContactRelevance,
    PeerRelevance,
    StrangerRelevance,
)
from .registry_document import RegistrySnapshot, RosterVisibility
from .routing import DeliveryScope
from .store_files import DependentStateTier, SingleFileStore
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .thread_owned_state import ThreadOwnedState
from .threads import Thread

if TYPE_CHECKING:
    from .history_views import HistoryViews
    from .message_bus import MessageBus
    from .registration import Registration


@dataclass(frozen=True, slots=True)
class RelationRevision:
    """An earlier note of a relation and when it was replaced or written."""

    note: str
    at: float


@dataclass(frozen=True, slots=True)
class RelationRecord:
    """One relation as a thread reads it: who made it, when, why and how it changed."""

    peer: str
    relevance: type[PeerRelevance]
    author: str
    # None when the relation records no time of its own.
    created_at: float | None
    note: str
    history: tuple[RelationRevision, ...]


class MutualThreadRelation(ABC):
    """Shared identity behavior for explicit and goal-derived mutual visibility."""

    # Each relation owns the relevance its two threads have toward each other.
    relevance: ClassVar[type[PeerRelevance]]
    source: ClassVar[str]
    # Why the two threads are related, in words an agent can read.
    note: str

    @abstractmethod
    def record(self, peer: ThreadIncarnation) -> RelationRecord:
        """Describe this relation to the thread on the other side of ``peer``."""

    @property
    @abstractmethod
    def owner_incarnation(self) -> ThreadIncarnation: ...

    @property
    @abstractmethod
    def peer_incarnation(self) -> ThreadIncarnation: ...

    @property
    def pair_identity(self) -> frozenset[ThreadIncarnation]:
        return frozenset((self.owner_incarnation, self.peer_incarnation))

    def incident(self, thread: Thread) -> bool:
        return thread.incarnation in self.pair_identity

    def entry_goal_contacts(self) -> tuple[GoalDerivedContact, ...]:
        return ()

    def entry(self, identity: ThreadIncarnation,
              person: presentation.ThreadView | presentation.NoThreadView,
              available: bool, current: RelationshipEntry | None) -> RelationshipEntry:
        return RelationshipEntry(
            identity.name, "thread", person if available else presentation.NoThreadView(),
            detail=f"{current.detail}\n{self.note}" if current and current.detail else self.note,
            available=available,
            sources=(*current.sources, self.source) if current else (self.source,),
            goal_contacts=(*current.goal_contacts, *self.entry_goal_contacts())
                          if current else self.entry_goal_contacts(),
        )

    def counterparts(self, thread: Thread) -> Iterator[ThreadIncarnation]:
        if self.owner_incarnation == thread.incarnation:
            yield self.peer_incarnation
        elif self.peer_incarnation == thread.incarnation:
            yield self.owner_incarnation


@dataclass(frozen=True, slots=True)
class CollaborationRevision(MutualThreadRelation):
    """One mutual contact, created by either participant and visible to both.

    Creation timestamps identify the registered incarnations. A new thread
    reusing a deleted name does not inherit that thread's work relationships.
    """

    owner: str
    peer: str
    owner_created: float
    peer_created: float
    note: str
    created_at: float
    updated_at: float
    relevance: ClassVar[type[PeerRelevance]] = CollaboratorRelevance
    source: ClassVar[str] = "explicit"

    def earlier_notes(self) -> tuple[RelationRevision, ...]:
        return ()

    def record(self, peer: ThreadIncarnation) -> RelationRecord:
        # The stored owner is the thread that created the collaboration.
        return RelationRecord(
            peer.name, self.relevance, self.owner, self.created_at, self.note,
            self.earlier_notes(),
        )

    @property
    def owner_incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.owner, self.owner_created)

    @property
    def peer_incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.peer, self.peer_created)

    def oriented(self, thread: Thread):
        if self.owner_incarnation == thread.incarnation:
            return self
        return replace(
            self,
            owner=self.peer,
            peer=self.owner,
            owner_created=self.peer_created,
            peer_created=self.owner_created,
        )

    def resolved(self, registry: RegistrySnapshot):
        return replace(
            self,
            owner=self.owner_incarnation.resolved(registry).name,
            peer=self.peer_incarnation.resolved(registry).name,
        )


@dataclass(frozen=True, slots=True)
class Collaboration(CollaborationRevision):
    # Earlier revisions are kept as history, never as parallel active edges.
    history: tuple[CollaborationRevision, ...] = ()

    def revision(self) -> CollaborationRevision:
        return CollaborationRevision(
            **{field.name: getattr(self, field.name) for field in fields(CollaborationRevision)}
        )

    def earlier_notes(self) -> tuple[RelationRevision, ...]:
        return tuple(
            RelationRevision(revision.note, revision.updated_at) for revision in self.history
        )

    def updated(self, note: str, now: float) -> Collaboration:
        return replace(self, note=note, updated_at=now, history=(*self.history, self.revision()))


@dataclass(frozen=True, slots=True)
class GoalDerivedContact(MutualThreadRelation):
    """Mutual *visibility* only; a mention never accepts work or dispatches a wake."""

    owner: str
    owner_created_at: float
    peer: str
    peer_created_at: float
    goal_id: GoalId
    text_revision: int
    relevance: ClassVar[type[PeerRelevance]] = GoalContactRelevance
    source: ClassVar[str] = "goal_mention"

    def entry_goal_contacts(self) -> tuple[GoalDerivedContact, ...]:
        return (self,)

    @property
    def note(self) -> str:
        return (
            f"Mentioned by {self.owner}'s goal {self.goal_id} "
            f"text revision {self.text_revision} (awareness only)"
        )

    def record(self, peer: ThreadIncarnation) -> RelationRecord:
        # The goal's owner wrote the mention; a mention records no time.
        return RelationRecord(peer.name, self.relevance, self.owner, None, self.note, ())

    @property
    def owner_incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.owner, self.owner_created_at)

    @property
    def peer_incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.peer, self.peer_created_at)


@dataclass(frozen=True, slots=True)
class GoalMentionDiagnostic:
    owner: str
    goal_id: GoalId
    text_revision: int
    token: str
    reason: type[GoalMentionBinding] | Literal["stale_incarnation"]


@dataclass(frozen=True, slots=True)
class RelationshipEntry:
    target: str
    kind: str
    person: presentation.ThreadView | presentation.NoThreadView
    sequence: int = 0
    timestamp: float = 0
    detail: str = ""
    available: bool = True
    sources: tuple[str, ...] = ()
    goal_contacts: tuple[GoalDerivedContact, ...] = ()

    @property
    def identity(self) -> tuple[str, str]:
        return self.kind, self.target

    def revalidates(self, earlier: RelationshipEntry) -> bool:
        """A fresh navigation target must retain its identity and incarnation."""
        if not self.available or self.identity != earlier.identity:
            return False
        return all(
            any(person.thread.incarnation == previous.thread.incarnation
                for person in self.person.present())
            for previous in earlier.person.present()
        )


@dataclass(frozen=True, slots=True)
class RelationshipGroup(DeclaredFamily, affix="RelationshipGroup"):
    """A declared group owns its identity, presentation and entry source."""

    entries: dict[str, RelationshipEntry]
    family_discriminator: ClassVar[str] = "key"
    title: ClassVar[str]
    empty_text: ClassVar[str]
    order: ClassVar[type[ThreadSort] | None] = None

    @classmethod
    @abstractmethod
    def sources(cls, inputs: RelationshipInputs) -> tuple[RelationshipEntrySource, ...]: ...

    @classmethod
    def build(cls, context: RelationshipContext,
              sources: tuple[RelationshipEntrySource, ...]) -> RelationshipGroup:
        entries: dict[str, RelationshipEntry] = {}
        for source in sources:
            entry = source.entry(context)
            entries[entry.target] = entry
        return cls.from_entries(entries, context)

    @classmethod
    def from_entries(cls, entries: dict[str, RelationshipEntry],
                     context: RelationshipContext) -> RelationshipGroup:
        return cls(entries)

    @classmethod
    def capture(cls, inputs: RelationshipInputs, views: HistoryViews) -> tuple[RelationshipGroup, ...]:
        """Retain original inputs once, then construct complete results on one roster cut."""
        selected = tuple((group, group.sources(inputs)) for group in cls.members_with(cls))
        names = dict.fromkeys(source.name for _, sources in selected for source in sources)
        people = MappingProxyType({view.thread.name: view for view in presentation.ThreadView.roster(
            inputs.registry, views.agents, views.goals, RosterVisibility(archived=True), names=names,
        )})
        context = RelationshipContext(
            **{item.name: getattr(inputs, item.name) for item in fields(RelationshipInputs)},
            people=people,
        )
        return tuple(group.build(context, sources) for group, sources in selected)

    def selectable(self) -> tuple[SortableRelationshipGroup, ...]:
        return ()


class RecentRelationshipGroup(RelationshipGroup):
    @classmethod
    def sources(cls, inputs: RelationshipInputs) -> tuple[RelationshipEntrySource, ...]:
        selected: dict[str, RecentEntrySource] = {}
        for message in reversed(inputs.messages):
            if not message.role.counts_as_conversation():
                continue
            for target in cls.targets(inputs, message):
                selected.setdefault(target.name, RecentEntrySource(target, message))
        return tuple(selected.values())

    @staticmethod
    @abstractmethod
    def targets(inputs: RelationshipInputs, message: MessageInput) -> tuple[NamedTarget, ...]: ...


class InboundRelationshipGroup(RecentRelationshipGroup):
    title = "Last inbound"
    empty_text = "No recent inbound"

    @staticmethod
    def targets(inputs: RelationshipInputs, message: MessageInput) -> tuple[NamedTarget, ...]:
        sender = inputs.registry.canonical_name(message.sender)
        if sender == inputs.thread.name or not inputs.delivery.delivers(message.sender, message.target):
            return ()
        return tuple(RelationshipEntrySource.canonical_target(name, inputs.registry)
                     for name in CommandTarget.named(message.target).inbound_relations(sender))


class OutboundRelationshipGroup(RecentRelationshipGroup):
    title = "Last outbound"
    empty_text = "No recent outbound"

    @staticmethod
    def targets(inputs: RelationshipInputs, message: MessageInput) -> tuple[NamedTarget, ...]:
        if inputs.registry.canonical_name(message.sender) != inputs.thread.name:
            return ()
        return (RelationshipEntrySource.canonical_target(message.target, inputs.registry),)


class ParentRelationshipGroup(RelationshipGroup):
    title = "Parent fork"
    empty_text = "Not forked"

    @classmethod
    def sources(cls, inputs: RelationshipInputs) -> tuple[RelationshipEntrySource, ...]:
        return tuple(
            NamedEntrySource(RelationshipEntrySource.canonical_target(name, inputs.registry))
            for name in inputs.thread.parent.names()
        )


@dataclass(frozen=True, slots=True)
class SortableRelationshipGroup(RelationshipGroup):
    """Only this capability has saved ordering, defaults and ordered entries."""

    order: type[ThreadSort] = field()
    default_order: ClassVar[type[ThreadSort]]

    @classmethod
    def selected_order(
        cls, orders: Mapping[type[SortableRelationshipGroup], type[ThreadSort]],
    ) -> type[ThreadSort]:
        return orders.get(cls, cls.default_order)

    @classmethod
    def from_entries(cls, entries: dict[str, RelationshipEntry],
                     context: RelationshipContext) -> SortableRelationshipGroup:
        order = cls.selected_order(context.orders)

        def key(item: RelationshipEntry) -> tuple[float, float, str]:
            return item.person.sort_key(order, item.target, context.sent.get(item.target, 0))

        return cls({entry.target: entry for entry in sorted(entries.values(), key=key)}, order)

    def selectable(self) -> tuple[SortableRelationshipGroup, ...]:
        return (self,)

    @classmethod
    def ordered(cls, document: RelationshipDocument, owner: Thread,
                order: type[ThreadSort]) -> RelationshipDocument:
        row = RelationshipOrder(owner.name, owner.incarnation.wire_birth, cls, order)
        return replace(document, orders=tuple(
            saved for saved in document.orders if saved.identity != row.identity
        ) + (row,))


class ChildrenRelationshipGroup(SortableRelationshipGroup):
    title = "Children"
    empty_text = "No children"
    default_order = CreatedThreadSort

    @classmethod
    def sources(cls, inputs: RelationshipInputs) -> tuple[RelationshipEntrySource, ...]:
        return tuple(
            NamedEntrySource(RelationshipEntrySource.canonical_target(child.name, inputs.registry))
            for child in inputs.registry.children(inputs.thread.name)
        )


class CollaboratingRelationshipGroup(SortableRelationshipGroup):
    title = "Collaborating"
    empty_text = "No active collaborations"
    default_order = LastActivityThreadSort

    @classmethod
    def sources(cls, inputs: RelationshipInputs) -> tuple[RelationshipEntrySource, ...]:
        selected: dict[ThreadIncarnation, list[MutualThreadRelation]] = {}
        for relation in (*inputs.edges, *inputs.goal_contacts):
            for identity in relation.counterparts(inputs.thread):
                selected.setdefault(identity, []).append(relation)
        return tuple(MutualEntrySource(identity, tuple(relations))
                     for identity, relations in selected.items())


@dataclass(frozen=True, slots=True)
class RelationshipInputs:
    """One captured selection basis, before acquiring any person presentation."""

    registry: RegistrySnapshot
    thread: Thread
    messages: tuple[MessageInput, ...]
    delivery: DeliveryScope
    edges: tuple[Collaboration, ...]
    goal_contacts: tuple[GoalDerivedContact, ...]
    orders: Mapping[type[SortableRelationshipGroup], type[ThreadSort]]
    sent: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class RelationshipContext(RelationshipInputs):
    people: Mapping[str, presentation.ThreadView]

    def entry(self, target: NamedTarget, *, sequence: int = 0, timestamp: float = 0,
              detail: str = "") -> RelationshipEntry:
        return RelationshipEntry(
            target.name, target.kind, self.people.get(target.name, presentation.NoThreadView()),
            sequence, timestamp, detail,
        )


class RelationshipEntrySource(ABC):
    """An original group input, never an unfinished public entry or a wire record."""

    __slots__ = ()

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def entry(self, context: RelationshipContext) -> RelationshipEntry: ...

    @staticmethod
    def canonical_target(name: str, registry: RegistrySnapshot) -> NamedTarget:
        target = CommandTarget.named(name)
        return replace(target, name=target.selected_name(registry))


@dataclass(frozen=True, slots=True)
class NamedEntrySource(RelationshipEntrySource):
    target: NamedTarget

    @property
    def name(self) -> str:
        return self.target.name

    def entry(self, context: RelationshipContext) -> RelationshipEntry:
        return context.entry(self.target)


@dataclass(frozen=True, slots=True)
class RecentEntrySource(NamedEntrySource):
    message: MessageInput

    def entry(self, context: RelationshipContext) -> RelationshipEntry:
        return context.entry(
            self.target, sequence=self.message.seq, timestamp=self.message.timestamp,
            detail=f"{self.message.sender} → {self.message.target}\n{self.message.body[:240]}",
        )


@dataclass(frozen=True, slots=True)
class MutualEntrySource(RelationshipEntrySource):
    identity: ThreadIncarnation
    relations: tuple[MutualThreadRelation, ...]

    @property
    def name(self) -> str:
        return self.identity.name

    def entry(self, context: RelationshipContext) -> RelationshipEntry:
        person = context.people.get(self.name, presentation.NoThreadView())
        available = any(view.thread.incarnation == self.identity for view in person.present())
        first, *remaining = self.relations
        entry = first.entry(self.identity, person, available, None)
        for relation in remaining:
            entry = relation.entry(self.identity, person, available, entry)
        return entry


@dataclass(frozen=True, slots=True)
class ThreadCommsSnapshot:
    owner: str
    root: str
    groups: tuple[RelationshipGroup, ...]
    history_limited: bool
    history_messages: int
    incoming_basis: str = "Current delivery scope, independent of read markers"
    unresolved_goal_mentions: tuple[GoalMentionDiagnostic, ...] = ()

    @property
    def groups_by_type(self) -> dict[type[RelationshipGroup], RelationshipGroup]:
        """Index the actual groups by exact nominal identity, never by a base capability."""
        return {type(group): group for group in self.groups}

    def current_entry(self, group: type[RelationshipGroup],
                      earlier: RelationshipEntry) -> RelationshipEntry | None:
        if (current := self.groups_by_type.get(group)) is None:
            return None
        entry = current.entries.get(earlier.target)
        return entry if entry is not None and entry.revalidates(earlier) else None

    @property
    def sortable_groups(self) -> tuple[SortableRelationshipGroup, ...]:
        return tuple(selected for group in self.groups for selected in group.selectable())


@dataclass(frozen=True, slots=True)
class PeerRelation:
    """How much one peer matters to a thread, and why."""

    peer: ThreadIncarnation
    relevance: type[PeerRelevance]
    records: tuple[RelationRecord, ...] = ()

    @property
    def note(self) -> str:
        return "\n".join(record.note for record in self.records if record.note)

    @staticmethod
    def order(first: PeerRelation, second: PeerRelation) -> int:
        """More derived relevance first, read from the hierarchy; then name."""
        if first.relevance is not second.relevance:
            if issubclass(first.relevance, second.relevance):
                return -1
            if issubclass(second.relevance, first.relevance):
                return 1
        return (first.peer.name > second.peer.name) - (first.peer.name < second.peer.name)


@dataclass(frozen=True, slots=True)
class Contacts:
    """One registry/relationship snapshot, independent of bus history."""

    explicit: tuple[Collaboration, ...]
    visible: tuple[RelationshipEntry, ...]
    diagnostics: tuple[GoalMentionDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class RelationshipOrder:
    owner: str
    owner_created: float
    group: type[SortableRelationshipGroup]
    order: type[ThreadSort]

    @property
    def incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.owner, self.owner_created)

    @property
    def identity(self) -> tuple[ThreadIncarnation, type[SortableRelationshipGroup]]:
        return self.incarnation, self.group

    def resolved(self, registry: RegistrySnapshot) -> RelationshipOrder:
        return replace(self, owner=self.incarnation.resolved(registry).name)


@dataclass(frozen=True, slots=True)
class RelationshipDocument:
    collaborations: Annotated[tuple[Collaboration, ...], field_codec.WireRequired()] = (
        ()
    )
    orders: Annotated[tuple[RelationshipOrder, ...], field_codec.WireRequired()] = ()
    version: Annotated[Literal[2], field_codec.WireRequired()] = 2

    def __post_init__(self):
        pairs = [edge.pair_identity for edge in self.collaborations]
        if len(pairs) != len(set(pairs)):
            raise ValueError("Duplicate collaboration pairs require explicit migration")
        order_keys = [row.identity for row in self.orders]
        if len(order_keys) != len(set(order_keys)):
            raise ValueError("Duplicate relationship sort preferences")

    def resolved(self, registry: RegistrySnapshot) -> RelationshipDocument:
        # Rename aliases are current domain data; deleted/rebound names stay historical.

        return replace(
            self,
            collaborations=tuple(edge.resolved(registry) for edge in self.collaborations),
            orders=tuple(row.resolved(registry) for row in self.orders),
        )


@dataclass(frozen=True)
class RelationshipEditContext:
    document: RelationshipDocument
    first: Thread
    peer_name: str
    second: Thread | None

    @property
    def pairs(self) -> tuple[Collaboration, ...]:
        return tuple(
            edge
            for edge in self.document.collaborations
            if {edge.owner, edge.peer} == {self.first.name, self.peer_name}
            and edge.incident(self.first)
        )

    @property
    def active(self) -> Collaboration | None:
        return next(
            (
                edge
                for edge in self.pairs
                if self.second is not None
                and self.second.incarnation in edge.counterparts(self.first)
            ),
            None,
        )

    @property
    def existing(self) -> Collaboration | None:
        return self.active or next(iter(self.pairs), None)

    def require_peer(self) -> Thread:
        if self.second is None:
            raise UnregisteredThreadError(f"Thread {self.peer_name!r} is not registered.")
        self.second.role.require_executable(RelationViolationError(
            f"Thread {self.peer_name!r} is human; collaborations relate agents"
        ))
        if self.first.name == self.second.name:
            raise ValueError("A thread cannot collaborate with itself")
        if self.existing is not None and self.active is None:
            raise ValueError(
                "Peer identity was replaced; explicitly remove the unavailable "
                "collaboration before adding a new one"
            )
        return self.second

    def publish(self, changed: Collaboration) -> tuple[RelationshipDocument, Collaboration]:
        active = self.active
        if changed is active:
            return self.document, changed.oriented(self.first)
        return replace(
            self.document,
            collaborations=tuple(
                edge for edge in self.document.collaborations if edge is not active
            )
            + (changed,),
        ), changed.oriented(self.first)


@dataclass(frozen=True)
class RelationshipEdit[Result](
    Command[RelationshipEditContext, tuple[RelationshipDocument, Result]],
    DeclaredFamily, affix="RelationshipEdit",
):
    note: str = ""

    def __post_init__(self):
        if len(self.note) > 2000:
            raise ValueError("Collaboration notes are limited to 2000 characters")


class RetainingRelationshipEdit(RelationshipEdit[Collaboration]):
    def apply(self, ctx: RelationshipEditContext) -> tuple[RelationshipDocument, Collaboration]:
        peer = ctx.require_peer()
        return ctx.publish(self.change(ctx, peer))

    @abstractmethod
    def change(self, ctx: RelationshipEditContext, peer: Thread) -> Collaboration: ...


class AddRelationshipEdit(RetainingRelationshipEdit):
    def change(self, ctx: RelationshipEditContext, peer: Thread) -> Collaboration:
        if ctx.active is not None:
            return ctx.active
        now = time.time()
        return Collaboration(
            ctx.first.name, peer.name, ctx.first.incarnation.wire_birth, peer.incarnation.wire_birth, self.note, now, now
        )


class UpdateRelationshipEdit(RetainingRelationshipEdit):
    def change(self, ctx: RelationshipEditContext, peer: Thread) -> Collaboration:
        active = ctx.active
        if active is None:
            raise ValueError("Collaboration does not exist")
        return active.updated(self.note, time.time())


class RemoveRelationshipEdit(RelationshipEdit[None]):
    def apply(self, ctx: RelationshipEditContext) -> tuple[RelationshipDocument, None]:
        existing = ctx.existing
        if existing is None:
            return ctx.document, None
        return (
            replace(
                ctx.document,
                collaborations=tuple(
                    edge
                    for edge in ctx.document.collaborations
                    if edge.pair_identity != existing.pair_identity
                ),
            ),
            None,
        )


class RelationshipStore(SingleFileStore, ThreadOwnedState, CarriedDocument[RelationshipDocument], DependentStateTier):
    """One current durable typed schema; retired formats are rejected."""

    filename = "relationships.json"
    json_indent = 2
    json_suffix = "\n"

    record_type = RelationshipDocument

    def empty(self) -> RelationshipDocument:
        return RelationshipDocument()

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """A collaboration or order ends with either deleted incarnation."""
        deleted = {thread.incarnation for thread in threads}

        def remove(
            document: RelationshipDocument,
        ) -> DocumentChange[RelationshipDocument, RelationshipDocument]:
            collaborations = tuple(
                edge for edge in document.collaborations
                if not {edge.owner_incarnation, edge.peer_incarnation} & deleted
            )
            orders = tuple(order for order in document.orders if order.incarnation not in deleted)
            if (collaborations, orders) == (document.collaborations, document.orders):
                return DocumentChange.to(document)
            return DocumentChange.to(replace(document, collaborations=collaborations, orders=orders))

        self.update(remove)


class ThreadRelationships:
    """One root-scoped service, retained by Comms; no timers or owner actions."""

    RECENT_MESSAGES = 512

    def __init__(self, root: Path, registry: Registration, bus: MessageBus, views: HistoryViews):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.views = views
        self.store = RelationshipStore.at(root)

    def revision(self) -> presentation.WireRevision:
        """What a snapshot derives from; equal revisions show the same thing."""
        return self.views.revision()

    def collaborations(self, owner: str) -> tuple[Collaboration, ...]:
        registry = self.registry.snapshot()
        thread = self.registry.require(owner)
        edges = self.store.read().resolved(registry).collaborations
        return tuple(edge.oriented(thread) for edge in edges if edge.incident(thread))

    @staticmethod
    def _goal_contacts(
        registry: RegistrySnapshot, thread: Thread,
    ) -> tuple[tuple[GoalDerivedContact, ...], tuple[GoalMentionDiagnostic, ...]]:
        """The contacts touching ``thread`` and its own unresolved mentions, resolved from
        active goals in ONE registry snapshot; never changes an explicit contact or delivery."""
        contacts: list[GoalDerivedContact] = []
        diagnostics: list[GoalMentionDiagnostic] = []
        for owner in registry.threads.active_goal_owners():
            for goal in owner.goal.active():
                ThreadRelationships._project_goal_contacts(
                    goal, owner, registry, contacts, diagnostics,
                )
        return (tuple(row for row in contacts if row.incident(thread)),
                tuple(row for row in diagnostics if row.owner == thread.name))

    @staticmethod
    def _project_goal_contacts(goal: Goal, owner: Thread, registry: RegistrySnapshot,
                               contacts: list[GoalDerivedContact],
                               diagnostics: list[GoalMentionDiagnostic]) -> None:
        source = goal.mention_source
        # Missing historical evidence grants no contact or current-name binding.
        if source is None or not source.matches(goal, owner, registry):
            return
        for binding in source.bindings:
            projected, issues = binding.project(registry, owner, goal, source)
            contacts.extend(projected)
            diagnostics.extend(issues)

    def goal_contacts(
        self, owner: str
    ) -> tuple[tuple[GoalDerivedContact, ...], tuple[GoalMentionDiagnostic, ...]]:
        return self._goal_contacts(self.registry.snapshot(), self.registry.require(owner))

    def peer_relations(self, owner: str) -> tuple[PeerRelation, ...]:
        """Every other registered thread with its relevance to ``owner``, most relevant first."""
        registry = self.registry.snapshot()
        thread = self.registry.require(owner)
        edges = self.store.read().resolved(registry).collaborations
        contacts, _ = self._goal_contacts(registry, thread)
        granted: dict[ThreadIncarnation, list[type[PeerRelevance]]] = {}
        records: dict[ThreadIncarnation, list[RelationRecord]] = {}
        for peer in registry.threads.values():
            if peer.incarnation == thread.incarnation:
                continue
            granted[peer.incarnation] = [StrangerRelevance]
            # The builtin channels hold every thread; a shared tag is a shared channel.
            if thread.tags & peer.tags:
                granted[peer.incarnation].append(ChannelmateRelevance)
        relation: MutualThreadRelation
        for relation in (*edges, *contacts):
            for identity in relation.counterparts(thread):
                if identity in granted:
                    granted[identity].append(relation.relevance)
                    records.setdefault(identity, []).append(relation.record(identity))
        return tuple(sorted(
            (
                PeerRelation(
                    identity,
                    PeerRelevance.most_derived(members),
                    tuple(records.get(identity, ())),
                )
                for identity, members in granted.items()
            ),
            key=cmp_to_key(PeerRelation.order),
        ))

    def context_relations(self, owner: str) -> tuple[PeerRelation, ...]:
        """Open contacts, the parent and current-incarnation recent direct work."""
        registry = self.registry.snapshot()
        thread = registry.require(owner)
        relations = self.peer_relations(owner)
        messages, _ = self._recent_messages()
        correspondents: dict[str, float] = {}
        for message in messages:
            peer = message.role.response_policy(message).context_peer(
                thread.name, registry.canonical_name(message.sender),
                registry.canonical_name(message.target))
            if peer is not None:
                correspondents[peer] = max(correspondents.get(peer, 0), message.timestamp)
        return tuple(r for r in relations if r.records or thread.parent.named(r.peer.name)
                     or correspondents.get(r.peer.name, 0) >= max(
                         thread.created_at, r.peer.require_recorded().created_at))

    def relevance(self, owner: str, peer: str) -> type[PeerRelevance]:
        """The most derived relevance ``peer`` has to ``owner``."""
        identity = self.registry.require(peer).incarnation
        for relation in self.peer_relations(owner):
            if relation.peer == identity:
                return relation.relevance
        raise ValueError("A thread has no relevance to itself")

    def contacts(self, owner: str) -> Contacts:
        """Combine manual and bound-goal contacts without reading a bus row."""
        registry = self.registry.snapshot()
        thread = self.registry.require(owner)
        edges = self.store.read().resolved(registry).collaborations
        explicit = tuple(edge.oriented(thread) for edge in edges if edge.incident(thread))
        contacts, diagnostics = self._goal_contacts(registry, thread)
        visible: dict[ThreadIncarnation, RelationshipEntry] = {}
        for relation in (*explicit, *contacts):
            for identity in relation.counterparts(thread):
                visible[identity] = relation.entry(
                    identity, presentation.NoThreadView(), identity.current(registry),
                    visible.get(identity),
                )
        return Contacts(
            explicit,
            tuple(sorted(visible.values(), key=lambda row: (row.target, not row.available))),
            diagnostics,
        )

    def edit(
        self, owner: str, action: type[RelationshipEdit], peer: str, note: str = ""
    ) -> Collaboration | None:
        command = action(note)
        first = self.registry.require(owner)
        first.role.require_executable(ValueError("Collaborations relate agent threads"))
        registry = self.registry.snapshot()
        peer = registry.canonical_name(peer)

        def change(
            document: RelationshipDocument,
        ) -> DocumentChange[RelationshipDocument, Collaboration | None]:
            resolved = document.resolved(registry)
            changed, result = command.apply(
                RelationshipEditContext(resolved, first, peer, registry.threads.get(peer))
            )
            return DocumentChange(document if changed == document else changed, result)

        return self.store.update(change)

    def set_order(self, owner: str, group: type[SortableRelationshipGroup],
                  order: type[ThreadSort]) -> type[ThreadSort]:
        thread = self.registry.require(owner)
        registry = self.registry.snapshot()
        self.store.update(
            lambda document: DocumentChange.to(group.ordered(document.resolved(registry), thread, order))
        )
        return order

    def _recent_messages(self) -> tuple[tuple[MessageInput, ...], bool]:
        """One bounded sequence page, on a single committed read snapshot."""
        with self.bus.bus.reading() as source:
            messages = source.input_page(source.revision() + 1, self.RECENT_MESSAGES + 1)
        return (
            tuple(reversed(messages[:self.RECENT_MESSAGES])),
            len(messages) > self.RECENT_MESSAGES,
        )

    def snapshot(self, owner: str) -> ThreadCommsSnapshot:
        # One coherent identity/metadata basis; wire payload work is bounded.
        registry = self.registry.snapshot()
        thread = registry.require(owner)
        state = self.store.read().resolved(registry)
        edges = state.collaborations
        goal_contacts, goal_diagnostics = self._goal_contacts(registry, thread)
        delivery = DeliveryScope.capture(thread.name, registry, self.views.channels.catalog.read())
        messages, limited = self._recent_messages()

        orders = {
            row.group: row.order for row in state.orders if row.incarnation == thread.incarnation
        }
        sent: dict[str, float] = {}
        for order in {
            group.selected_order(orders)
            for group in SortableRelationshipGroup.members_with(SortableRelationshipGroup)
        }:
            sent.update(order.sent_timestamps(self.views, registry=registry))
        inputs = RelationshipInputs(
            registry, thread, messages, delivery, edges, goal_contacts,
            MappingProxyType(orders), MappingProxyType(sent),
        )
        return ThreadCommsSnapshot(
            thread.name,
            str(self.root.resolve()),
            RelationshipGroup.capture(inputs, self.views),
            limited,
            len(messages),
            unresolved_goal_mentions=goal_diagnostics,
        )
