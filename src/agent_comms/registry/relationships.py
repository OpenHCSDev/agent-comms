"""Explicit work relationships and a bounded, read-only thread sidebar projection.

Collaboration is cooperative metadata, not a dispatch or delivery instruction.
The recent wire window follows the core's current delivery scope; it does not
claim historical proof that a model consumed an old channel message.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import cmp_to_key
from typing import Literal

from agent_comms import presentation
from agent_comms.display_order import CreatedThreadSort, LastActivityThreadSort, ThreadSort
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.messages import Message, MessageInput, MessageRole
from agent_comms.messaging.routing import DeliveryScope
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import GoalId
from agent_comms.peer_relevance import (
    ChannelmateRelevance,
    PeerRelevance,
    StrangerRelevance,
)
from agent_comms.planning.command import Command
from agent_comms.planning.goals import Goal, GoalMentionBinding
from agent_comms.registry.peer_relation import PeerRelation, RelationRecord
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.relationship_source import RelationshipSource
from agent_comms.registry.relationship_store import (
    Collaboration,
    GoalDerivedContact,
    MutualThreadRelation,
    RelationshipContext,
    RelationshipDocument,
    RelationshipEntry,
    RelationshipGroup,
    RelationshipStore,
    SortableRelationshipGroup,
)
from agent_comms.registry.roster_visibility import RosterVisibility
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import Thread
from agent_comms.storage.locked_store import DocumentChange
from agent_comms.transport.tool_result import ToolResult


@dataclass(frozen=True, slots=True)
class GoalMentionDiagnostic:
    owner: str
    goal_id: GoalId
    text_revision: int
    token: str
    reason: type[GoalMentionBinding] | Literal["stale_incarnation"]


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
        return tuple(context.entry(name) for name in context.thread.parent.names())


class ChildrenRelationshipGroup(SortableRelationshipGroup):
    title = "Children"
    empty_text = "No children"
    default_order = CreatedThreadSort

    @classmethod
    def supply(cls, context: RelationshipContext) -> tuple[RelationshipEntry, ...]:
        return tuple(
            context.entry(child.name)
            for child in context.registry.threads.values()
            for parent in child.parent.names()
            if context.registry.canonical_name(parent) == context.thread.name
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
class ThreadCommsSnapshot(ToolResult):
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
class Contacts:
    """One registry/relationship snapshot, independent of bus history."""

    explicit: tuple[Collaboration, ...]
    visible: tuple[RelationshipEntry, ...]
    diagnostics: tuple[GoalMentionDiagnostic, ...]


@dataclass(frozen=True)
class RelationshipEditContext:
    document: RelationshipDocument
    first: Thread
    peer_name: str
    second: PeerThread

    @property
    def pairs(self) -> tuple[Collaboration, ...]:
        return tuple(
            edge
            for edge in self.document.collaborations
            if {edge.owner, edge.peer} == {self.first.name, self.peer_name}
            and edge.incident(self.first)
        )

    @property
    def collaboration(self) -> PairCollaboration:
        """Decode the pair's one collaboration state once, from its edges."""
        pairs = self.pairs
        for edge in pairs:
            if self.second.holds(edge.counterpart(self.first)):
                return ActiveCollaboration(edge)
        return ReplacedPeerCollaboration(pairs[0]) if pairs else NoCollaboration()

    def require_peer(self) -> Thread:
        peer = self.second.require(self.peer_name)
        peer.role.require_executable(RelationViolationError(
            f"Thread {self.peer_name!r} is human; collaborations relate agents"
        ))
        if self.first.name == peer.name:
            raise ValueError("A thread cannot collaborate with itself")
        self.collaboration.require_current_peer()
        return peer

    def publish(self, changed: Collaboration) -> tuple[RelationshipDocument, Collaboration]:
        return self.collaboration.published(self.document, changed), changed.oriented(self.first)


class PeerThread(ABC):
    """The named peer: registered under its current incarnation, or not registered."""

    @classmethod
    def named(cls, threads: Mapping[str, Thread], name: str) -> PeerThread:
        return RegisteredPeer(threads[name]) if name in threads else UnregisteredPeer()

    @abstractmethod
    def require(self, name: str) -> Thread: ...

    @abstractmethod
    def holds(self, incarnation: object) -> bool: ...


@dataclass(frozen=True)
class RegisteredPeer(PeerThread):
    thread: Thread

    def require(self, name: str) -> Thread:
        return self.thread

    def holds(self, incarnation: object) -> bool:
        return incarnation == self.thread.incarnation


class UnregisteredPeer(PeerThread):
    def require(self, name: str) -> Thread:
        raise UnregisteredThreadError(f"Thread {name!r} is not registered.")

    def holds(self, incarnation: object) -> bool:
        return False


class PairCollaboration(ABC):
    """A peer pair's collaboration: none, active, or held by a replaced peer identity."""

    def require_current_peer(self) -> None:
        """Only an edge naming a replaced peer identity refuses an edit."""

    @abstractmethod
    def added(
        self, ctx: RelationshipEditContext, peer: Thread, note: str,
    ) -> Collaboration: ...

    @abstractmethod
    def updated(self, note: str) -> Collaboration: ...

    @abstractmethod
    def removed(self, document: RelationshipDocument) -> RelationshipDocument: ...

    @abstractmethod
    def published(
        self, document: RelationshipDocument, changed: Collaboration,
    ) -> RelationshipDocument: ...


class NoCollaboration(PairCollaboration):
    """The pair has no edge: adding creates one, updating has nothing to update."""

    def added(self, ctx: RelationshipEditContext, peer: Thread, note: str) -> Collaboration:
        now = time.time()
        return Collaboration(
            ctx.first.name, peer.name, ctx.first.incarnation.wire_birth,
            peer.incarnation.wire_birth, note, now, now,
        )

    def updated(self, note: str) -> Collaboration:
        raise ValueError("Collaboration does not exist")

    def removed(self, document: RelationshipDocument) -> RelationshipDocument:
        return document

    def published(
        self, document: RelationshipDocument, changed: Collaboration,
    ) -> RelationshipDocument:
        return replace(document, collaborations=(*document.collaborations, changed))


@dataclass(frozen=True)
class HeldCollaboration(PairCollaboration):
    """The pair holds this edge; removing it drops the pair."""

    edge: Collaboration

    def removed(self, document: RelationshipDocument) -> RelationshipDocument:
        return replace(document, collaborations=tuple(
            edge for edge in document.collaborations
            if edge.pair_identity != self.edge.pair_identity
        ))


@dataclass(frozen=True)
class ActiveCollaboration(HeldCollaboration):
    """The edge names the peer's current incarnation."""

    def added(self, ctx: RelationshipEditContext, peer: Thread, note: str) -> Collaboration:
        return self.edge

    def updated(self, note: str) -> Collaboration:
        return self.edge.updated(note, time.time())

    def published(
        self, document: RelationshipDocument, changed: Collaboration,
    ) -> RelationshipDocument:
        if changed is self.edge:
            return document
        return replace(document, collaborations=(
            *(edge for edge in document.collaborations if edge is not self.edge), changed,
        ))


@dataclass(frozen=True)
class ReplacedPeerCollaboration(HeldCollaboration, NoCollaboration):
    """An edge survives a replaced peer identity: removable, never edited in place."""

    def require_current_peer(self) -> None:
        raise ValueError(
            "Peer identity was replaced; explicitly remove the unavailable "
            "collaboration before adding a new one"
        )


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
        return ctx.collaboration.added(ctx, peer, self.note)


class UpdateRelationshipEdit(RetainingRelationshipEdit):
    def change(self, ctx: RelationshipEditContext, peer: Thread) -> Collaboration:
        return ctx.collaboration.updated(self.note)


class RemoveRelationshipEdit(RelationshipEdit[None]):
    def apply(self, ctx: RelationshipEditContext) -> tuple[RelationshipDocument, None]:
        return ctx.collaboration.removed(ctx.document), None


class ThreadRelationships:
    """Relationships read configuration from their one supplied view owner."""

    RECENT_MESSAGES = 512

    def __init__(self, views: RelationshipSource):
        self.views = views
        self.store = RelationshipStore.at(views.root)

    def revision(self) -> presentation.WireRevision:
        """What a snapshot derives from; equal revisions show the same thing."""
        return self.views.revision()

    def collaborations(self, owner: str) -> tuple[Collaboration, ...]:
        registry = self.views.registry.snapshot()
        thread = self.views.registry.require(owner)
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
        for owner in (
            executor
            for thread in registry.threads.values()
            for executor in thread.role.executor(thread)
        ):
            for goal in owner.goal.active():
                ThreadRelationships._project_goal_contacts(goal, owner, registry, contacts, diagnostics)
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
            projected, issues = binding.project(
                registry, owner, goal, source, GoalDerivedContact, GoalMentionDiagnostic,
            )
            contacts.extend(projected)
            diagnostics.extend(issues)

    def goal_contacts(
        self, owner: str
    ) -> tuple[tuple[GoalDerivedContact, ...], tuple[GoalMentionDiagnostic, ...]]:
        return self._goal_contacts(self.views.registry.snapshot(), self.views.registry.require(owner))

    def peer_relations(self, owner: str) -> tuple[PeerRelation, ...]:
        """Every other registered thread with its relevance to ``owner``, most relevant first."""
        registry = self.views.registry.snapshot()
        thread = self.views.registry.require(owner)
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
        registry = self.views.registry.snapshot()
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
        identity = self.views.registry.require(peer).incarnation
        for relation in self.peer_relations(owner):
            if relation.peer == identity:
                return relation.relevance
        raise ValueError("A thread has no relevance to itself")

    def contacts(self, owner: str) -> Contacts:
        """Combine manual and bound-goal contacts without reading a bus row."""
        registry = self.views.registry.snapshot()
        thread = self.views.registry.require(owner)
        edges = self.store.read().resolved(registry).collaborations
        explicit = tuple(edge.oriented(thread) for edge in edges if edge.incident(thread))
        contacts, diagnostics = self._goal_contacts(registry, thread)
        visible: dict[ThreadIncarnation, RelationshipEntry] = {}
        for relation in (*explicit, *contacts):
            identity = relation.counterpart(thread)
            visible[identity] = relation.entry(
                identity, None, identity.current(registry), visible.get(identity),
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
        first = self.views.registry.require(owner)
        first.role.require_executable(ValueError("Collaborations relate agent threads"))
        registry = self.views.registry.snapshot()
        peer = registry.canonical_name(peer)

        def change(
            document: RelationshipDocument,
        ) -> DocumentChange[RelationshipDocument, Collaboration | None]:
            resolved = document.resolved(registry)
            changed, result = command.apply(
                RelationshipEditContext(resolved, first, peer, PeerThread.named(registry.threads, peer))
            )
            return DocumentChange(document if changed == document else changed, result)

        return self.store.update(change)

    def set_order(self, owner: str, group: type[SortableRelationshipGroup],
                  order: type[ThreadSort]) -> type[ThreadSort]:
        thread = self.views.registry.require(owner)
        registry = self.views.registry.snapshot()
        self.store.update(
            lambda document: DocumentChange.to(group.ordered(document.resolved(registry), thread, order))
        )
        return order

    def _recent_messages(self) -> tuple[tuple[Message, ...], bool]:
        """Each conversation's newest message in the recent window, on one committed snapshot.

        Every group here derives its targets from a message's sender and target alone, and keeps
        the newest message per target, so the older messages of a conversation decided nothing:
        reading them cost 400 microseconds each, 205 ms of the window on this bus.
        """
        with self.views.bus.bus.reading() as source:
            through = source.revision()
            messages = source.newest_per_conversation(
                MessageInput.render(MessageRole.stored_conversations()), (),
                after=through - self.RECENT_MESSAGES, through=through)
        return messages, through > self.RECENT_MESSAGES

    def snapshot(self, owner: str) -> ThreadCommsSnapshot:
        # One coherent identity/metadata basis; wire payload work is bounded.
        registry = self.views.registry.snapshot()
        thread = registry.require(owner)
        state = self.store.read().resolved(registry)
        edges = state.collaborations
        goal_contacts, goal_diagnostics = self._goal_contacts(registry, thread)
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
            str(self.views.root.resolve()),
            tuple(group.build(context) for group in RelationshipGroup.members_with(RelationshipGroup)),
            limited,
            len(messages),
            unresolved_goal_mentions=goal_diagnostics,
        )
