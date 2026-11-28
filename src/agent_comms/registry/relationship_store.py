from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field, fields, replace
from typing import Annotated, ClassVar, Literal

from agent_comms.display_order import ThreadSort
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.messages import Message
from agent_comms.messaging.routing import DeliveryScope
from agent_comms.nominal import field_codec
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import GoalId
from agent_comms.peer_relevance import CollaboratorRelevance, GoalContactRelevance, PeerRelevance
from agent_comms.registry.peer_relation import RelationRecord, RelationRevision
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.relationship_file import RelationshipFile
from agent_comms.registry.thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from agent_comms.registry.thread_owned_state import ThreadOwnedState
from agent_comms.registry.threads import Thread
from agent_comms.storage.locked_store import DocumentChange
from agent_comms.storage.maintenance_barrier import CarriedDocument
from agent_comms.storage.store_files import DependentStateTier, SingleFileStore
from agent_comms.thread_views import ThreadView


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

    def entry(self, identity: ThreadIncarnation, person: ThreadView | None,
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
class RelationshipEntry:
    target: str
    kind: str
    person: ThreadView | None = None
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

    @classmethod
    def is_group_type(cls, candidate: object) -> bool:
        """Whether ``candidate`` is a class in this family — used by views that
        store a group class in a widget's user-data slot and later ask whether
        a given cell carries a group (vs an entry or None). Keeps callers from
        restating the `isinstance(x, type) and issubclass(x, RelationshipGroup)`
        pair at every site."""
        return isinstance(candidate, type) and issubclass(candidate, cls)

    def selectable(self) -> tuple[SortableRelationshipGroup, ...]:
        return ()


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
        row = RelationshipOrder(owner.name, owner.incarnation.wire_birth, cls, order)
        return replace(document, orders=tuple(
            saved for saved in document.orders if saved.identity != row.identity
        ) + (row,))


@dataclass(frozen=True, slots=True)
class RelationshipContext:
    registry: RegistrySnapshot
    thread: Thread
    people: dict[str, ThreadView]
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


class RelationshipStore(
    SingleFileStore, ThreadOwnedState, CarriedDocument[RelationshipDocument], DependentStateTier,
    RelationshipFile,
):
    """One current durable typed schema; retired formats are rejected."""

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
