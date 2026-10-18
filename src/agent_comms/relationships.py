"""Explicit work relationships and a bounded, read-only thread sidebar projection.

Collaboration is cooperative metadata, not a dispatch or delivery instruction.
The recent wire window follows the core's current delivery scope; it does not
claim historical proof that a model consumed an old channel message.
"""

from __future__ import annotations

from .wire_log import CurrentSnapshotOpening

import json
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field, fields, replace
from functools import cmp_to_key
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, ClassVar, Literal

from .channel_targets import CommandTarget
from .command import Command
from .declared_family import DeclaredFamily
from .display_order import CreatedThreadSort, LastActivityThreadSort, ThreadSort
from .errors import RelationViolationError, UnregisteredThreadError
from .goals import GoalMentionBinding
from .locked_store import LockedStore, RootFile
from .peer_relevance import (
    ChannelmateRelevance,
    CollaboratorRelevance,
    GoalContactRelevance,
    PeerRelevance,
    StrangerRelevance,
)
from . import presentation
from .wire_record import WireRecord
from .messages import Message
from .private_path import FileRevision
from .registry_document import RegistrySnapshot, RosterVisibility
from .routing import DeliveryScope
from .store_files import SharedLock, _store_lock
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

    def entry(self, identity: ThreadIncarnation, person: presentation.ThreadView | None,
              available: bool, current: RelationshipEntry | None) -> RelationshipEntry:
        return RelationshipEntry(
            identity.name, "thread", person if available else None,
            detail=f"{current.detail}\n{self.note}" if current and current.detail else self.note,
            available=available,
            sources=(*current.sources, self.source) if current else (self.source,),
            goal_contacts=(*current.goal_contacts, *self.entry_goal_contacts())
                          if current else self.entry_goal_contacts(),
        )

    def counterpart(self, thread: Thread) -> ThreadIncarnation | None:
        if self.owner_incarnation == thread.incarnation:
            return self.peer_incarnation
        if self.peer_incarnation == thread.incarnation:
            return self.owner_incarnation
        return None


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
    goal_id: str
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
    goal_id: str
    text_revision: int
    token: str
    reason: type[GoalMentionBinding] | Literal["stale_incarnation"]


@dataclass(frozen=True, slots=True)
class RelationshipEntry:
    target: str
    kind: str
    person: presentation.ThreadView | None = None
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
        if earlier.person is None:
            return True
        return (self.person is not None
                and self.person.thread.incarnation == earlier.person.thread.incarnation)


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
    def supply(cls, context: RelationshipContext) -> tuple[RelationshipEntry, ...]: ...

    @classmethod
    def build(cls, context: RelationshipContext) -> RelationshipGroup:
        return cls({entry.target: entry for entry in cls.supply(context)})

    def selectable(self) -> tuple[SortableRelationshipGroup, ...]:
        return ()


class RecentRelationshipGroup(RelationshipGroup):
    @classmethod
    def supply(cls, context: RelationshipContext) -> tuple[RelationshipEntry, ...]:
        entries = {}
        for message in reversed(context.messages):
            if not message.role.counts_as_conversation():
                continue
            for target in cls.targets(context, message):
                entries.setdefault(target, context.entry(
                    target, message=message,
                    detail=f"{message.sender} → {message.target}\n{message.body[:240]}",
                ))
        return tuple(entries.values())

    @staticmethod
    @abstractmethod
    def targets(context: RelationshipContext, message: Message) -> tuple[str, ...]: ...


class InboundRelationshipGroup(RecentRelationshipGroup):
    title = "Last inbound"
    empty_text = "No recent inbound"

    @staticmethod
    def targets(context: RelationshipContext, message: Message) -> tuple[str, ...]:
        sender = context.registry.canonical_name(message.sender)
        if sender == context.thread.name or not context.delivery.delivers(message.sender, message.target):
            return ()
        return CommandTarget.named(message.target).inbound_relations(sender)


class OutboundRelationshipGroup(RecentRelationshipGroup):
    title = "Last outbound"
    empty_text = "No recent outbound"

    @staticmethod
    def targets(context: RelationshipContext, message: Message) -> tuple[str, ...]:
        if context.registry.canonical_name(message.sender) != context.thread.name:
            return ()
        return (CommandTarget.named(message.target).canonical_name(context.registry.canonical_name),)


class ParentRelationshipGroup(RelationshipGroup):
    title = "Parent fork"
    empty_text = "Not forked"

    @classmethod
    def supply(cls, context: RelationshipContext) -> tuple[RelationshipEntry, ...]:
        return (context.entry(context.thread.parent),) if context.thread.parent else ()


@dataclass(frozen=True, slots=True)
class SortableRelationshipGroup(RelationshipGroup):
    """Only this capability has saved ordering, defaults and ordered entries."""

    order: type[ThreadSort] = field()
    default_order: ClassVar[type[ThreadSort]]

    @classmethod
    def selected_order(cls, context: RelationshipContext) -> type[ThreadSort]:
        return context.orders.get(cls, cls.default_order)

    @classmethod
    def build(cls, context: RelationshipContext) -> SortableRelationshipGroup:
        order = cls.selected_order(context)

        def key(item: RelationshipEntry) -> tuple[float, float, str]:
            person = item.person
            return order.key(
                item.target,
                person.thread.created_at if person else 0,
                person.activity.timestamp if person else 0,
                context.sent.get(item.target, 0) if person else 0,
            )

        return cls({entry.target: entry for entry in sorted(cls.supply(context), key=key)}, order)

    def selectable(self) -> tuple[SortableRelationshipGroup, ...]:
        return (self,)

    @classmethod
    def ordered(cls, document: RelationshipDocument, owner: Thread,
                order: type[ThreadSort]) -> RelationshipDocument:
        row = RelationshipOrder(owner.name, owner.created_at, cls, order)
        return replace(document, orders=tuple(
            saved for saved in document.orders if saved.identity != row.identity
        ) + (row,))


class ChildrenRelationshipGroup(SortableRelationshipGroup):
    title = "Children"
    empty_text = "No children"
    default_order = CreatedThreadSort

    @classmethod
    def supply(cls, context: RelationshipContext) -> tuple[RelationshipEntry, ...]:
        return tuple(
            context.entry(child.name)
            for child in context.registry.threads.values()
            if child.parent and context.registry.canonical_name(child.parent) == context.thread.name
        )


class CollaboratingRelationshipGroup(SortableRelationshipGroup):
    title = "Collaborating"
    empty_text = "No active collaborations"
    default_order = LastActivityThreadSort

    @classmethod
    def supply(cls, context: RelationshipContext) -> tuple[RelationshipEntry, ...]:
        entries: dict[ThreadIncarnation, RelationshipEntry] = {}
        for relation in (*context.edges, *context.goal_contacts):
            identity = relation.counterpart(context.thread)
            if identity is None:
                continue
            person = context.people.get(identity.name)
            available = person is not None and person.thread.incarnation == identity
            entries[identity] = relation.entry(identity, person, available, entries.get(identity))
        return tuple(entries.values())


@dataclass(frozen=True, slots=True)
class RelationshipContext:
    registry: RegistrySnapshot
    thread: Thread
    people: dict[str, presentation.ThreadView]
    messages: tuple[Message, ...]
    delivery: DeliveryScope
    edges: tuple[Collaboration, ...]
    goal_contacts: tuple[GoalDerivedContact, ...]
    orders: dict[type[SortableRelationshipGroup], type[ThreadSort]]
    sent: dict[str, float]

    def entry(self, name: str, *, message: Message | None = None,
              detail: str = "") -> RelationshipEntry:
        target = CommandTarget.named(name)
        name = target.canonical_name(self.registry.canonical_name)
        return RelationshipEntry(
            name, target.kind, self.people.get(name),
            message.seq if message else 0, message.timestamp if message else 0, detail,
        )


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
    collaborations: tuple[Collaboration, ...] = field(default=(), metadata={"wire_required": True})
    orders: tuple[RelationshipOrder, ...] = field(default=(), metadata={"wire_required": True})
    version: Literal[2] = field(default=2, metadata={"wire_required": True})

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
                and edge.counterpart(self.first) == self.second.incarnation
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

    def publish(self, changed: Collaboration):
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
class RelationshipEdit(Command, DeclaredFamily, affix="RelationshipEdit"):
    note: str = ""

    def __post_init__(self):
        if len(self.note) > 2000:
            raise ValueError("Collaboration notes are limited to 2000 characters")

    @abstractmethod
    def apply(self, ctx: RelationshipEditContext): ...


class RetainingRelationshipEdit(RelationshipEdit):
    def apply(self, ctx: RelationshipEditContext):
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
            ctx.first.name, peer.name, ctx.first.created_at, peer.created_at, self.note, now, now
        )


class UpdateRelationshipEdit(RetainingRelationshipEdit):
    def change(self, ctx: RelationshipEditContext, peer: Thread) -> Collaboration:
        active = ctx.active
        if active is None:
            raise ValueError("Collaboration does not exist")
        return active.updated(self.note, time.time())


class RemoveRelationshipEdit(RelationshipEdit):
    def apply(self, ctx: RelationshipEditContext):
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


class RelationshipStore(RootFile, ThreadOwnedState, LockedStore[RelationshipDocument]):
    """One current durable typed schema; retired formats are rejected."""

    lock_rank = 5
    filename = "relationships.json"
    json_indent = 2
    json_suffix = "\n"

    @property
    def record_type(self) -> type[RelationshipDocument]:
        return RelationshipDocument

    def empty(self) -> RelationshipDocument:
        return RelationshipDocument()

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """A collaboration or order ends with either deleted incarnation."""
        deleted = {thread.incarnation for thread in threads}

        def remove(document: RelationshipDocument) -> RelationshipDocument:
            collaborations = tuple(
                edge for edge in document.collaborations
                if not {edge.owner_incarnation, edge.peer_incarnation} & deleted
            )
            orders = tuple(order for order in document.orders if order.incarnation not in deleted)
            if (collaborations, orders) == (document.collaborations, document.orders):
                return document
            return replace(document, collaborations=collaborations, orders=orders)

        self.update(remove)


class ThreadRelationships:
    """One root-scoped service, retained by Comms; no timers or owner actions."""

    RECENT_BYTES = 2 * 1024 * 1024
    RECENT_MESSAGES = 512

    def __init__(self, root: Path, registry: Registration, bus: MessageBus, views: HistoryViews):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.views = views
        self._wire_lock_path = root / "wire"
        self.store = RelationshipStore.at(root)
        self._lock = RLock()
        self._recent_revision: FileRevision | None = None
        self._recent: tuple[Message, ...] = ()
        self._limited = False

    def revision(self) -> presentation.WireRevision:
        """What a snapshot derives from; equal revisions show the same thing."""
        return self.views.revision()

    def collaborations(self, owner: str) -> tuple[Collaboration, ...]:
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            registry = self.registry.snapshot()
            thread = self.registry.require(owner)
            edges = self.store.read().resolved(registry).collaborations
            return tuple(edge.oriented(thread) for edge in edges if edge.incident(thread))

    @staticmethod
    def _goal_contacts(
        registry: RegistrySnapshot,
    ) -> tuple[tuple[GoalDerivedContact, ...], tuple[GoalMentionDiagnostic, ...]]:
        """Resolve only saved incarnations from active goals in ONE registry snapshot."""
        contacts: list[GoalDerivedContact] = []
        diagnostics: list[GoalMentionDiagnostic] = []
        for owner in (
            executor
            for thread in registry.threads.values()
            for executor in thread.role.executor(thread)
        ):
            goal = owner.goal.active()
            if not goal:
                continue
            source = goal.mention_source
            # Missing historical evidence grants no contact or current-name binding.
            if source is None:
                continue
            if not source.matches(goal, owner, registry):
                continue
            for binding in source.bindings:
                projected, issues = binding.project(registry, owner, goal, source)
                contacts.extend(projected)
                diagnostics.extend(issues)
        return tuple(contacts), tuple(diagnostics)

    def goal_contacts(
        self, owner: str
    ) -> tuple[tuple[GoalDerivedContact, ...], tuple[GoalMentionDiagnostic, ...]]:
        """Read-only mutual view; never changes an explicit contact or delivery."""
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            registry = self.registry.snapshot()
            thread = self.registry.require(owner)
            contacts, diagnostics = self._goal_contacts(registry)
            return (
                tuple(row for row in contacts if row.counterpart(thread) is not None),
                tuple(row for row in diagnostics if row.owner == thread.name),
            )

    def peer_relations(self, owner: str) -> tuple[PeerRelation, ...]:
        """Every other registered thread with its relevance to ``owner``, most relevant first."""
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            registry = self.registry.snapshot()
            thread = self.registry.require(owner)
            edges = self.store.read().resolved(registry).collaborations
            contacts, _ = self._goal_contacts(registry)
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
            identity = relation.counterpart(thread)
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
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
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
        return tuple(r for r in relations if r.records or r.peer.name == thread.parent
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
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            registry = self.registry.snapshot()
            thread = self.registry.require(owner)
            edges = self.store.read().resolved(registry).collaborations
            explicit = tuple(edge.oriented(thread) for edge in edges if edge.incident(thread))
            contacts, diagnostics = self._goal_contacts(registry)
            visible: dict[ThreadIncarnation, RelationshipEntry] = {}
            for relation in (*explicit, *contacts):
                identity = relation.counterpart(thread)
                if identity is None:
                    continue
                visible[identity] = relation.entry(
                    identity, None, identity.current(registry), visible.get(identity),
                )
            return Contacts(
                explicit,
                tuple(sorted(visible.values(), key=lambda row: (row.target, not row.available))),
                tuple(row for row in diagnostics if row.owner == thread.name),
            )

    def edit(
        self, owner: str, action: type[RelationshipEdit], peer: str, note: str = ""
    ) -> Collaboration | None:
        command = action(note)
        with _store_lock(self._wire_lock_path):
            first = self.registry.require(owner)
            first.role.require_executable(ValueError("Collaborations relate agent threads"))
            registry = self.registry.snapshot()
            peer = registry.canonical_name(peer)
            result = None

            def change(document: RelationshipDocument) -> RelationshipDocument:
                nonlocal result
                resolved = document.resolved(registry)
                changed, result = command.apply(
                    RelationshipEditContext(resolved, first, peer, registry.threads.get(peer))
                )
                return document if changed == document else changed

            self.store.update(change)
            return result

    def set_order(self, owner: str, group: type[SortableRelationshipGroup],
                  order: type[ThreadSort]) -> type[ThreadSort]:
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(owner)
            registry = self.registry.snapshot()
            self.store.update(
                lambda document: group.ordered(document.resolved(registry), thread, order)
            )
        return order

    def _recent_messages(self) -> tuple[tuple[Message, ...], bool]:
        """Decode a bounded tail from one fixed opened bus cut."""
        path = self.bus.log.path
        revision = FileRevision.capture(path)
        with self._lock:
            if revision == self._recent_revision:
                return self._recent, self._limited
            with self.bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as source:
                return self._opened_recent_messages(source)


    def _opened_recent_messages(self, source) -> tuple[tuple[Message, ...], bool]:
        if not source.changed_since(self._recent_revision):
            return self._recent, self._limited
        previous = self._recent_revision
        if (previous is not None and source.same_file(previous.identity)
                and source.boundary > previous.size
                and previous.holds_cut(source.revision, previous.size)):
            start = max(previous.size, source.boundary - self.RECENT_BYTES)
            data, start = source.tail_bytes(source.boundary - start)
            contiguous = start == previous.size
        else:
            data, start = source.tail_bytes(self.RECENT_BYTES)
            contiguous = False
        lines = data.splitlines(keepends=True)
        if start and not contiguous and lines:
            lines.pop(0)  # A bounded cut may begin in a partial record.
        messages = (self._recent if contiguous else ()) + tuple(
            message
            for line in lines
            if line.endswith(b"\n") and line.strip()
            for message in WireRecord.public_from_wire(json.loads(line)).messages()
        )
        limited = (self._limited if contiguous else bool(start)) or len(messages) > self.RECENT_MESSAGES
        messages = messages[-self.RECENT_MESSAGES :]
        self._recent_revision = source.revision if source.boundary else None
        self._recent, self._limited = messages, limited
        return messages, limited

    def snapshot(self, owner: str) -> ThreadCommsSnapshot:
        # One coherent identity/metadata basis; wire payload work is bounded.
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            registry = self.registry.snapshot()
            thread = registry.require(owner)
            state = self.store.read().resolved(registry)
            edges = state.collaborations
            goal_contacts, goal_diagnostics = self._goal_contacts(registry)
            delivery = DeliveryScope.capture(thread.name, registry, self.views.channels.catalog.read())
        people = {
            view.thread.name: view
            for view in self.views.thread_views(RosterVisibility(archived=True))
        }
        messages, limited = self._recent_messages()

        context = RelationshipContext(
            registry, thread, people, messages, delivery, edges, goal_contacts,
            {row.group: row.order for row in state.orders if row.incarnation == thread.incarnation},
            {},
        )
        for order in {
            group.selected_order(context)
            for group in SortableRelationshipGroup.members_with(SortableRelationshipGroup)
        }:
            context.sent.update(order.sent_timestamps(self.views, registry=registry))
        return ThreadCommsSnapshot(
            thread.name,
            str(self.root.resolve()),
            tuple(group.build(context) for group in RelationshipGroup.members_with(RelationshipGroup)),
            limited,
            len(messages),
            unresolved_goal_mentions=tuple(
                row for row in goal_diagnostics if row.owner == thread.name
            ),
        )
