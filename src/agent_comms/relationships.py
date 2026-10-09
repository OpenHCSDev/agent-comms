"""Explicit work relationships and a bounded, read-only thread sidebar projection.

Collaboration is cooperative metadata, not a dispatch or delivery instruction.
The recent wire window follows the core's current delivery scope; it does not
claim historical proof that a model consumed an old channel message.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Literal

from .channel_targets import is_channel_target
from .command import Command
from .declared_family import DeclaredFamily
from .display_order import ThreadSort
from .errors import UnregisteredThreadError
from .goals import GoalMentionBinding
from .locked_store import LockedStore
from .wire_record import WireRecord
from .messages import Message
from .presentation import ThreadView
from .registry_document import RegistrySnapshot
from .store_files import _store_lock, file_revision
from .thread_identity import ThreadIncarnation
from .thread_owned_state import ThreadOwnedState
from .threads import Thread

if TYPE_CHECKING:
    from .history_views import HistoryViews
    from .message_bus import MessageBus
    from .registration import Registration


class MutualThreadRelation(ABC):
    """Shared identity behavior for explicit and goal-derived mutual visibility."""

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

    @property
    def owner_incarnation(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.owner, self.owner_created)

    @property
    def peer_incarnation(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.peer, self.peer_created)

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
    # Previous records are immutable provenance, never active parallel edges.
    history: tuple[CollaborationRevision, ...] = ()

    def revision(self) -> CollaborationRevision:
        return CollaborationRevision(
            **{field.name: getattr(self, field.name) for field in fields(CollaborationRevision)}
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

    @property
    def owner_incarnation(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.owner, self.owner_created_at)

    @property
    def peer_incarnation(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.peer, self.peer_created_at)


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
    person: ThreadView | None = None
    sequence: int = 0
    timestamp: float = 0
    detail: str = ""
    available: bool = True
    sources: tuple[str, ...] = ()
    goal_contacts: tuple[GoalDerivedContact, ...] = ()


@dataclass(frozen=True, slots=True)
class RelationshipGroup:
    key: str
    title: str
    entries: tuple[RelationshipEntry, ...]
    order: ThreadSort | None = None


@dataclass(frozen=True, slots=True)
class ThreadCommsSnapshot:
    owner: str
    root: str
    groups: tuple[RelationshipGroup, ...]
    history_limited: bool
    history_messages: int
    incoming_basis: str = "Current delivery scope, independent of read markers"
    unresolved_goal_mentions: tuple[GoalMentionDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ContactProjection:
    """One registry/relationship snapshot, independent of bus history."""

    explicit: tuple[Collaboration, ...]
    visible: tuple[RelationshipEntry, ...]
    diagnostics: tuple[GoalMentionDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class RelationshipOrder:
    owner: str
    owner_created: float
    group: str
    order: ThreadSort

    @property
    def incarnation(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.owner, self.owner_created)

    @property
    def identity(self) -> tuple[ThreadIncarnation, str]:
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

    def ordered(self, owner: Thread, group: str, order: ThreadSort) -> RelationshipDocument:
        row = RelationshipOrder(owner.name, owner.created_at, group, order)
        rows = tuple(saved for saved in self.orders if saved.identity != row.identity)
        return replace(self, orders=(*rows, row))


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
        self.second.role.require_executable()
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


class RelationshipStore(ThreadOwnedState, LockedStore[RelationshipDocument]):
    """One current durable typed schema; retired formats are rejected."""

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
        self.store = RelationshipStore(root / RelationshipStore.filename)
        self._lock = RLock()
        self._recent_revision: tuple[int, int, int, int] | None = None
        self._recent: tuple[Message, ...] = ()
        self._limited = False

    def collaborations(self, owner: str) -> tuple[Collaboration, ...]:
        with _store_lock(self._wire_lock_path):
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
        for owner in registry.threads.values():
            goal = owner.active_goal
            if goal is None or not owner.role.executable:
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
        with _store_lock(self._wire_lock_path):
            registry = self.registry.snapshot()
            thread = self.registry.require(owner)
            contacts, diagnostics = self._goal_contacts(registry)
            return (
                tuple(row for row in contacts if row.counterpart(thread) is not None),
                tuple(row for row in diagnostics if row.owner == thread.name),
            )

    def contact_projection(self, owner: str) -> ContactProjection:
        """Combine manual and bound-goal contacts without reading a bus row."""
        with _store_lock(self._wire_lock_path):
            registry = self.registry.snapshot()
            thread = self.registry.require(owner)
            edges = self.store.read().resolved(registry).collaborations
            explicit = tuple(edge.oriented(thread) for edge in edges if edge.incident(thread))
            contacts, diagnostics = self._goal_contacts(registry)
            visible: dict[ThreadIncarnation, RelationshipEntry] = {}
            for edge in explicit:
                visible[edge.peer_incarnation] = RelationshipEntry(
                    edge.peer,
                    "thread",
                    detail=edge.note,
                    available=edge.peer_incarnation.current(registry),
                    sources=("explicit",),
                )
            for contact in contacts:
                identity = contact.counterpart(thread)
                if identity is None:
                    continue
                peer_name = identity.name
                current = visible.get(identity)
                provenance = (
                    f"Mentioned by {contact.owner}'s goal {contact.goal_id} "
                    f"text revision {contact.text_revision} (awareness only)"
                )
                visible[identity] = RelationshipEntry(
                    peer_name,
                    "thread",
                    detail=(
                        f"{current.detail}\n{provenance}"
                        if current and current.detail
                        else provenance
                    ),
                    available=True,  # _goal_contacts checked this exact live incarnation.
                    sources=(*current.sources, "goal_mention") if current else ("goal_mention",),
                    goal_contacts=(*current.goal_contacts, contact) if current else (contact,),
                )
            return ContactProjection(
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
            if not first.role.executable:
                raise ValueError("Collaborations relate agent threads")
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

    def set_order(self, owner: str, group: str, order: ThreadSort) -> ThreadSort:
        if group not in {"children", "collaborating"}:
            raise ValueError("This relationship group has no selectable sort")
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(owner)
            registry = self.registry.snapshot()
            self.store.update(
                lambda document: document.resolved(registry).ordered(thread, group, order)
            )
        return order

    def _recent_messages(self) -> tuple[tuple[Message, ...], bool]:
        """Read bounded tail bytes once per bus revision, using core envelopes."""
        path = self.root / "bus.jsonl"
        revision = file_revision(path)
        with self._lock:
            if revision == self._recent_revision:
                return self._recent, self._limited
            try:
                with _store_lock(path), path.open("rb") as stream:
                    size = stream.seek(0, 2)
                    start = max(0, size - self.RECENT_BYTES)
                    stream.seek(start)
                    data = stream.read(self.RECENT_BYTES)
            except FileNotFoundError:
                data, start = b"", 0
            lines = data.splitlines(keepends=True)
            if start and lines:
                lines.pop(0)  # The first record may be partial.
            complete = [line for line in lines if line.endswith(b"\n")]
            messages = tuple(
                message
                for line in complete
                if line.strip()
                for message in WireRecord.public_from_wire(json.loads(line)).messages()
            )
            limited = bool(start) or len(messages) > self.RECENT_MESSAGES
            messages = messages[-self.RECENT_MESSAGES :]
            self._recent_revision = revision
            self._recent, self._limited = messages, limited
            return messages, limited

    def snapshot(self, owner: str) -> ThreadCommsSnapshot:
        # One coherent identity/metadata basis; wire payload work is bounded.
        with _store_lock(self._wire_lock_path):
            thread = self.registry.require(owner)
            registry = self.registry.snapshot()
            state = self.store.read().resolved(registry)
            edges = state.collaborations
            goal_contacts, goal_diagnostics = self._goal_contacts(registry)
            delivery = self.bus._delivery_scope(thread.name)
        people = {
            view.thread.name: view
            for view in self.views.thread_views(show_stopped=True, show_archived=True)
        }
        messages, limited = self._recent_messages()

        def canonical(name: str) -> str:
            return registry.canonical_name(name)

        def entry(
            name: str, *, message: Message | None = None, detail: str = ""
        ) -> RelationshipEntry:
            name = name if is_channel_target(name) else canonical(name)
            return RelationshipEntry(
                name,
                "channel" if is_channel_target(name) else "thread",
                people.get(name),
                message.seq if message else 0,
                message.timestamp if message else 0,
                detail,
            )

        inbound: dict[str, RelationshipEntry] = {}
        outbound: dict[str, RelationshipEntry] = {}
        for message in reversed(messages):
            if message.notice or message.membership is not None:
                continue
            detail = f"{message.sender} → {message.target}\n{message.body[:240]}"
            if canonical(message.sender) == thread.name:
                key = (
                    canonical(message.target)
                    if not is_channel_target(message.target)
                    else message.target
                )
                outbound.setdefault(key, entry(message.target, message=message, detail=detail))
            elif delivery.delivers(message.sender, message.target):
                # Expose both the sender and the actual channel, not a fake author.
                inbound.setdefault(
                    canonical(message.sender), entry(message.sender, message=message, detail=detail)
                )
                if is_channel_target(message.target):
                    inbound.setdefault(
                        message.target, entry(message.target, message=message, detail=detail)
                    )

        orders = {"children": ThreadSort.CREATED, "collaborating": ThreadSort.LAST_ACTIVITY}
        for row in state.orders:
            if row.incarnation == thread.incarnation and row.group in orders:
                orders[row.group] = row.order
        # Existing declaration-owned timestamp sort, shared with channel members.
        sent = (
            self.views.last_sent_timestamps() if ThreadSort.LAST_MESSAGE in orders.values() else {}
        )

        def ordered(entries: list[RelationshipEntry], group: str) -> tuple[RelationshipEntry, ...]:
            def key(item: RelationshipEntry) -> tuple[float, float, str]:
                person = item.person
                return orders[group].key(
                    item.target,
                    person.thread.created_at if person else 0,
                    person.activity.timestamp if person else 0,
                    sent.get(item.target, 0) if person else 0,
                )

            return tuple(sorted(entries, key=key))

        children = [
            entry(child.name)
            for child in registry.threads.values()
            if child.parent and canonical(child.parent) == thread.name
        ]
        collaborating: dict[ThreadIncarnation, RelationshipEntry] = {}
        for edge in edges:
            identity = edge.counterpart(thread)
            if identity is None:
                continue
            other_name = identity.name
            person = people.get(other_name)
            available = person is not None and person.thread.incarnation == identity
            collaborating[identity] = RelationshipEntry(
                other_name,
                "thread",
                person if available else None,
                detail=edge.note,
                available=available,
                sources=("explicit",),
            )
        for contact in goal_contacts:
            contact_identity = contact.counterpart(thread)
            if contact_identity is None:
                continue
            other_name = contact_identity.name
            current = collaborating.get(contact_identity)
            person = people.get(other_name)
            available = person is not None and person.thread.incarnation == contact_identity
            provenance = (
                f"Mentioned by {contact.owner}'s goal {contact.goal_id} "
                f"text revision {contact.text_revision} (awareness only)"
            )
            collaborating[contact_identity] = RelationshipEntry(
                other_name,
                "thread",
                person if available else None,
                detail=(
                    f"{current.detail}\n{provenance}" if current and current.detail else provenance
                ),
                available=available,
                sources=(*current.sources, "goal_mention") if current else ("goal_mention",),
                goal_contacts=(*current.goal_contacts, contact) if current else (contact,),
            )
        return ThreadCommsSnapshot(
            thread.name,
            str(self.root.resolve()),
            (
                RelationshipGroup("inbound", "Last inbound", tuple(inbound.values())),
                RelationshipGroup("outbound", "Last outbound", tuple(outbound.values())),
                RelationshipGroup(
                    "parent", "Parent fork", (entry(thread.parent),) if thread.parent else ()
                ),
                RelationshipGroup(
                    "children", "Children", ordered(children, "children"), orders["children"]
                ),
                RelationshipGroup(
                    "collaborating",
                    "Collaborating",
                    ordered(list(collaborating.values()), "collaborating"),
                    orders["collaborating"],
                ),
            ),
            limited,
            len(messages),
            unresolved_goal_mentions=tuple(
                row for row in goal_diagnostics if row.owner == thread.name
            ),
        )
