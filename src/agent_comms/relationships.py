"""Explicit work relationships and a bounded, read-only thread sidebar projection.

Collaboration is cooperative metadata, not a dispatch or delivery instruction.
The recent wire window follows the core's current delivery scope; it does not
claim historical proof that a model consumed an old channel message.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Literal

from .channel_targets import is_channel_target
from .display_order import ThreadSort
from .errors import UnregisteredThreadError
from .locked_store import LockedStore
from .messages import Message
from .presentation import ThreadView
from .registry_document import RegistrySnapshot
from .store_files import _store_lock, file_revision
from .threads import Thread

if TYPE_CHECKING:
    from .history_views import HistoryViews
    from .message_bus import MessageBus
    from .registration import Registration


@dataclass(frozen=True, slots=True)
class CollaborationRevision:
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

    def incident(self, thread: Thread) -> bool:
        return (self.owner, self.owner_created) == (thread.name, thread.created_at) or (
            self.peer,
            self.peer_created,
        ) == (thread.name, thread.created_at)

    def counterpart(self, thread: Thread) -> tuple[str, float]:
        if (self.owner, self.owner_created) == (thread.name, thread.created_at):
            return self.peer, self.peer_created
        return self.owner, self.owner_created

    def oriented(self, thread: Thread):
        if (self.owner, self.owner_created) == (thread.name, thread.created_at):
            return self
        return replace(
            self,
            owner=self.peer,
            peer=self.owner,
            owner_created=self.peer_created,
            peer_created=self.owner_created,
        )

    @property
    def pair_identity(self) -> frozenset[tuple[str, float]]:
        return frozenset(((self.owner, self.owner_created), (self.peer, self.peer_created)))

    def resolved(self, registry: RegistrySnapshot):
        def current_name(name: str, created: float) -> str:
            canonical = registry.aliases.get(name, name)
            thread = registry.threads.get(canonical)
            return canonical if thread and thread.created_at == created else name

        return replace(
            self,
            owner=current_name(self.owner, self.owner_created),
            peer=current_name(self.peer, self.peer_created),
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
class GoalDerivedContact:
    """Mutual *visibility* only; a mention never accepts work or dispatches a wake."""

    owner: str
    owner_created_at: float
    peer: str
    peer_created_at: float
    goal_id: str
    text_revision: int


@dataclass(frozen=True, slots=True)
class GoalMentionDiagnostic:
    owner: str
    goal_id: str
    text_revision: int
    token: str
    reason: str


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


@dataclass(frozen=True, slots=True)
class RelationshipDocument:
    collaborations: tuple[Collaboration, ...] = field(default=(), metadata={"wire_required": True})
    orders: tuple[RelationshipOrder, ...] = field(default=(), metadata={"wire_required": True})
    version: Literal[2] = field(default=2, metadata={"wire_required": True})

    def __post_init__(self):
        pairs = [edge.pair_identity for edge in self.collaborations]
        if len(pairs) != len(set(pairs)):
            raise ValueError("Duplicate collaboration pairs require explicit migration")
        order_keys = [(row.owner, row.owner_created, row.group) for row in self.orders]
        if len(order_keys) != len(set(order_keys)):
            raise ValueError("Duplicate relationship sort preferences")

    def resolved(self, registry: RegistrySnapshot) -> RelationshipDocument:
        # Rename aliases are current domain data; deleted/rebound names stay historical.
        def resolve_order(row: RelationshipOrder) -> RelationshipOrder:
            name = registry.aliases.get(row.owner, row.owner)
            owner = registry.threads.get(name)
            return (
                replace(row, owner=name) if owner and owner.created_at == row.owner_created else row
            )

        return replace(
            self,
            collaborations=tuple(edge.resolved(registry) for edge in self.collaborations),
            orders=tuple(resolve_order(row) for row in self.orders),
        )

    def edit(
        self, first: Thread, peer: str, second: Thread | None, action: str, note: str
    ) -> tuple[RelationshipDocument, Collaboration | None]:
        pairs = tuple(
            edge
            for edge in self.collaborations
            if {edge.owner, edge.peer} == {first.name, peer} and edge.incident(first)
        )
        active = next(
            (
                edge
                for edge in pairs
                if second is not None
                and edge.counterpart(first) == (second.name, second.created_at)
            ),
            None,
        )
        unavailable = next((edge for edge in pairs if edge is not active), None)
        existing = active or unavailable
        if action == "remove":
            if existing is None:
                return self, None
            return (
                replace(
                    self,
                    collaborations=tuple(
                        edge
                        for edge in self.collaborations
                        if edge.pair_identity != existing.pair_identity
                    ),
                ),
                None,
            )
        if second is None:
            raise UnregisteredThreadError(f"Thread {peer!r} is not registered.")
        if not second.role.executable:
            raise ValueError("Collaborations relate agent threads")
        if first.name == second.name:
            raise ValueError("A thread cannot collaborate with itself")
        if unavailable and active is None:
            raise ValueError(
                "Peer identity was replaced; explicitly remove the unavailable "
                "collaboration before adding a new one"
            )
        if action == "update" and active is None:
            raise ValueError("Collaboration does not exist")
        if action == "add" and active is not None:
            return self, active.oriented(first)
        now = time.time()
        changed = (
            active.updated(note, now)
            if active
            else Collaboration(
                first.name, second.name, first.created_at, second.created_at, note, now, now
            )
        )
        return replace(
            self,
            collaborations=tuple(edge for edge in self.collaborations if edge is not active)
            + (changed,),
        ), changed.oriented(first)

    def ordered(self, owner: Thread, group: str, order: ThreadSort) -> RelationshipDocument:
        row = RelationshipOrder(owner.name, owner.created_at, group, order)
        rows = tuple(
            saved
            for saved in self.orders
            if (saved.owner, saved.owner_created, saved.group)
            != (owner.name, owner.created_at, group)
        )
        return replace(self, orders=(*rows, row))


class RelationshipStore(LockedStore[RelationshipDocument]):
    """One current typed schema. Version1 conversion is an explicit offline operation."""

    filename = "relationships.json"
    json_indent = 2
    json_suffix = "\n"

    @property
    def record_type(self) -> type[RelationshipDocument]:
        return RelationshipDocument

    def empty(self) -> RelationshipDocument:
        return RelationshipDocument()


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

    def revision(self) -> tuple:
        return (
            *self.views.revision().files,
            file_revision(self.store.path),
            self.views.revision().expiry_tick,
        )

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
            goal = owner.goal
            if goal is None or not goal.state.active or not owner.role.executable:
                continue
            source = goal.mention_source
            # Missing historical evidence grants no contact or current-name binding.
            if source is None:
                continue
            if (
                source.goal_id != goal.id
                or source.text_digest != hashlib.sha256(goal.text.encode("utf-8")).hexdigest()
                or source.owner_created_at != owner.created_at
                or registry.aliases.get(source.owner_name, source.owner_name) != owner.name
                or source.text_revision > goal.revision
            ):
                continue
            for binding in source.bindings:
                if binding.resolution != "resolved":
                    diagnostics.append(
                        GoalMentionDiagnostic(
                            owner.name,
                            goal.id,
                            source.text_revision,
                            binding.token,
                            binding.resolution,
                        )
                    )
                    continue
                bound_name = binding.peer_name
                name = registry.aliases.get(bound_name, bound_name) if bound_name else None
                peer = registry.threads.get(name) if name is not None else None
                if (
                    peer is None
                    or peer.created_at != binding.peer_created_at
                    or not peer.role.executable
                    or peer.created_at == owner.created_at
                ):
                    diagnostics.append(
                        GoalMentionDiagnostic(
                            owner.name,
                            goal.id,
                            source.text_revision,
                            binding.token,
                            "stale_incarnation",
                        )
                    )
                    continue
                contacts.append(
                    GoalDerivedContact(
                        owner.name,
                        owner.created_at,
                        peer.name,
                        peer.created_at,
                        goal.id,
                        source.text_revision,
                    )
                )
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
                tuple(
                    row
                    for row in contacts
                    if thread.created_at in {row.owner_created_at, row.peer_created_at}
                ),
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
            visible: dict[tuple[str, float], RelationshipEntry] = {}
            for edge in explicit:
                live = registry.threads.get(edge.peer)
                visible[(edge.peer, edge.peer_created)] = RelationshipEntry(
                    edge.peer,
                    "thread",
                    detail=edge.note,
                    available=live is not None and live.created_at == edge.peer_created,
                    sources=("explicit",),
                )
            for contact in contacts:
                if (contact.owner, contact.owner_created_at) == (thread.name, thread.created_at):
                    peer_name, peer_created = contact.peer, contact.peer_created_at
                elif (contact.peer, contact.peer_created_at) == (thread.name, thread.created_at):
                    peer_name, peer_created = contact.owner, contact.owner_created_at
                else:
                    continue
                identity = peer_name, peer_created
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

    def edit(self, owner: str, action: str, peer: str, note: str = "") -> Collaboration | None:
        if action not in {"add", "update", "remove"}:
            raise ValueError("Expected add, update or remove")
        if len(note) > 2000:
            raise ValueError("Collaboration notes are limited to 2000 characters")
        with _store_lock(self._wire_lock_path):
            first = self.registry.require(owner)
            if not first.role.executable:
                raise ValueError("Collaborations relate agent threads")
            registry = self.registry.snapshot()
            peer = registry.aliases.get(peer, peer)
            result = None

            def change(document: RelationshipDocument) -> RelationshipDocument:
                nonlocal result
                resolved = document.resolved(registry)
                changed, result = resolved.edit(
                    first, peer, registry.threads.get(peer), action, note
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
            limited = bool(start) or len(complete) > self.RECENT_MESSAGES
            messages = tuple(
                Message.from_wire(json.loads(line))
                for line in complete[-self.RECENT_MESSAGES :]
                if line.strip()
            )
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
            return registry.aliases.get(name, name)

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
            if (
                canonical(row.owner) == thread.name
                and row.owner_created == thread.created_at
                and row.group in orders
            ):
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
        collaborating: dict[tuple[str, float], RelationshipEntry] = {}
        for edge in edges:
            if not edge.incident(thread):
                continue
            other_name, other_created = edge.counterpart(thread)
            person = people.get(other_name)
            available = person is not None and person.thread.created_at == other_created
            collaborating[(other_name, other_created)] = RelationshipEntry(
                other_name,
                "thread",
                person if available else None,
                detail=edge.note,
                available=available,
                sources=("explicit",),
            )
        for contact in goal_contacts:
            if (contact.owner, contact.owner_created_at) == (thread.name, thread.created_at):
                other_name, other_created = contact.peer, contact.peer_created_at
            elif (contact.peer, contact.peer_created_at) == (thread.name, thread.created_at):
                other_name, other_created = contact.owner, contact.owner_created_at
            else:
                continue
            contact_identity = other_name, other_created
            current = collaborating.get(contact_identity)
            person = people.get(other_name)
            available = person is not None and person.thread.created_at == other_created
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
