"""Explicit work relationships and a bounded, read-only thread sidebar projection.

Collaboration is cooperative metadata, not a dispatch or delivery instruction.
The recent wire window follows the core's current delivery scope; it does not
claim historical proof that a model consumed an old channel message.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, replace
from threading import RLock
from typing import TYPE_CHECKING

from .declarations import (
    Message,
    ThreadSort,
    ThreadView,
    _atomic_write_text,
    _store_lock,
    file_revision,
    is_channel_target,
)

if TYPE_CHECKING:
    from .operations import Comms


@dataclass(frozen=True, slots=True)
class Collaboration:
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


@dataclass(frozen=True, slots=True)
class RelationshipEntry:
    target: str
    kind: str
    person: ThreadView | None = None
    sequence: int = 0
    timestamp: float = 0
    detail: str = ""
    available: bool = True


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


class ThreadRelationships:
    """One root-scoped service, retained by Comms; no timers or owner actions."""

    RECENT_BYTES = 2 * 1024 * 1024
    RECENT_MESSAGES = 512

    def __init__(self, comms: Comms):
        self.comms = comms
        self.path = comms.root / "relationships.json"
        self._lock = RLock()
        self._recent_revision = None
        self._recent: tuple[Message, ...] = ()
        self._limited = False

    def revision(self) -> tuple:
        return (*self.comms.revision().files, file_revision(self.path),
                self.comms.revision().expiry_tick)

    def _load(self) -> dict:
        try:
            state = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"version": 1, "collaborations": [], "orders": []}
        if state.get("version") != 1:
            raise ValueError("Unsupported relationship store version")
        return state

    def _save(self, state: dict) -> None:
        _atomic_write_text(self.path, json.dumps(state, indent=2) + "\n", fsync_parent=True)

    def _canonical_edges(self, state: dict, registry) -> list[Collaboration]:
        edges = []
        for raw in state["collaborations"]:
            edge = Collaboration(**raw)
            owner = registry.aliases.get(edge.owner, edge.owner)
            peer = registry.aliases.get(edge.peer, edge.peer)
            first, second = registry.threads.get(owner), registry.threads.get(peer)
            # Normalize surviving aliases, but NEVER filter persistent records
            # through today's registry. Missing/replaced endpoints remain
            # copyable historical declarations, including their work notes.
            edges.append(replace(
                edge,
                owner=owner if first and first.created_at == edge.owner_created else edge.owner,
                peer=peer if second and second.created_at == edge.peer_created else edge.peer,
            ))
        return edges

    @staticmethod
    def _incident(edge: Collaboration, thread) -> bool:
        return ((edge.owner, edge.owner_created) == (thread.name, thread.created_at)
                or (edge.peer, edge.peer_created) == (thread.name, thread.created_at))

    @staticmethod
    def _counterpart(edge: Collaboration, thread) -> tuple[str, float]:
        if (edge.owner, edge.owner_created) == (thread.name, thread.created_at):
            return edge.peer, edge.peer_created
        return edge.owner, edge.owner_created

    @staticmethod
    def _orient(edge: Collaboration, thread) -> Collaboration:
        if (edge.owner, edge.owner_created) == (thread.name, thread.created_at):
            return edge
        return Collaboration(
            thread.name, edge.owner, thread.created_at, edge.owner_created,
            edge.note, edge.created_at, edge.updated_at,
        )

    @staticmethod
    def _pair_identity(edge: Collaboration) -> frozenset[tuple[str, float]]:
        return frozenset(((edge.owner, edge.owner_created),
                          (edge.peer, edge.peer_created)))

    @staticmethod
    def _unique_edges(edges: list[Collaboration]) -> list[Collaboration]:
        """Project legacy opposite-direction rows as one pair without losing notes.

        Deduplication is read-only; unrelated edits retain both raw records.
        A deliberate update/remove may replace/end that pair.
        """
        groups: dict[frozenset[tuple[str, float]], list[Collaboration]] = {}
        for edge in edges:
            key = ThreadRelationships._pair_identity(edge)
            groups.setdefault(key, []).append(edge)
        result = []
        for pair in groups.values():
            newest = max(pair, key=lambda edge: (edge.updated_at, edge.owner, edge.peer))
            if len(pair) == 1:
                result.append(newest)
            else:
                notes = tuple(dict.fromkeys(edge.note for edge in pair if edge.note))
                result.append(replace(
                    newest, note="\n".join(notes),
                    created_at=min(edge.created_at for edge in pair),
                ))
        return result

    def collaborations(self, owner: str) -> tuple[Collaboration, ...]:
        with _store_lock(self.comms._wire_lock_path), _store_lock(self.path):
            registry = self.comms.registry.snapshot()
            thread = self.comms.registry.require(owner)
            edges = self._unique_edges(self._canonical_edges(self._load(), registry))
            return tuple(self._orient(edge, thread)
                         for edge in edges
                         if self._incident(edge, thread))

    def edit(self, owner: str, action: str, peer: str, note: str = "") -> Collaboration | None:
        if action not in {"add", "update", "remove"}:
            raise ValueError("Expected add, update or remove")
        if len(note) > 2000:
            raise ValueError("Collaboration notes are limited to 2000 characters")
        with _store_lock(self.comms._wire_lock_path), _store_lock(self.path):
            first = self.comms.registry.require(owner)
            if not first.role.executable:
                raise ValueError("Collaborations relate agent threads")
            registry = self.comms.registry.snapshot()
            canonical_peer = registry.aliases.get(peer, peer)
            state = self._load()
            edges = self._canonical_edges(state, registry)
            pair = [edge for edge in edges
                    if {edge.owner, edge.peer} == {first.name, canonical_peer}]
            second_live = registry.threads.get(canonical_peer)
            active = [edge for edge in pair if self._incident(edge, first)
                      and second_live is not None
                      and self._counterpart(edge, first) == (second_live.name,
                                                            second_live.created_at)]
            unavailable = [edge for edge in pair if self._incident(edge, first)
                           and edge not in active]
            existing = (active or unavailable or [None])[0]
            if action == "remove":
                # Either participant can end the single shared relationship.
                # Prefer a live incarnation, leaving historical notes intact.
                # A reused caller name cannot remove another incarnation's row.
                if existing is not None:
                    identity = self._pair_identity(existing)
                    state["collaborations"] = [asdict(edge) for edge in edges if
                                               self._pair_identity(edge) != identity]
                    self._save(state)
                return None
            second = self.comms.registry.require(peer)
            if not second.role.executable:
                raise ValueError("Collaborations relate agent threads")
            if first.name == second.name:
                raise ValueError("A thread cannot collaborate with itself")
            if unavailable and not active:
                raise ValueError(
                    "Peer identity was replaced; explicitly remove the unavailable "
                    "collaboration before adding a new one"
                )
            if action == "update" and not active:
                raise ValueError("Collaboration does not exist")
            if action == "add" and active:
                return self._orient(self._unique_edges(active)[0], first)
            now = time.time()
            existing = active[0] if active else None
            result = Collaboration(
                existing.owner if existing else first.name,
                existing.peer if existing else second.name,
                existing.owner_created if existing else first.created_at,
                existing.peer_created if existing else second.created_at,
                note,
                min(edge.created_at for edge in active) if active else now, now,
            )
            edges = [edge for edge in edges if edge not in active]
            edges.append(result)
            state["collaborations"] = [asdict(edge) for edge in edges]
            self._save(state)
            return self._orient(result, first)

    def set_order(self, owner: str, group: str, order: ThreadSort) -> ThreadSort:
        if group not in {"children", "collaborating"}:
            raise ValueError("This relationship group has no selectable sort")
        order = ThreadSort(order)
        with _store_lock(self.comms._wire_lock_path), _store_lock(self.path):
            thread = self.comms.registry.require(owner)
            registry = self.comms.registry.snapshot()
            state = self._load()
            state["orders"] = [row for row in state["orders"] if not (
                registry.aliases.get(row["owner"], row["owner"]) == thread.name
                and row["owner_created"] == thread.created_at
                and row["group"] == group
            )]
            state["orders"].append({"owner": thread.name, "owner_created": thread.created_at,
                                    "group": group, "order": order.value})
            self._save(state)
        return order

    def _recent_messages(self) -> tuple[tuple[Message, ...], bool]:
        """Read bounded tail bytes once per bus revision, using core envelopes."""
        path = self.comms.root / "bus.jsonl"
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
            messages = tuple(Message.from_wire(json.loads(line))
                             for line in complete[-self.RECENT_MESSAGES:] if line.strip())
            self._recent_revision = revision
            self._recent, self._limited = messages, limited
            return messages, limited

    def snapshot(self, owner: str) -> ThreadCommsSnapshot:
        # One coherent identity/metadata basis; wire payload work is bounded.
        with _store_lock(self.comms._wire_lock_path), _store_lock(self.path):
            thread = self.comms.registry.require(owner)
            registry = self.comms.registry.snapshot()
            state = self._load()
            edges = self._canonical_edges(state, registry)
            delivery = self.comms.bus._delivery_scope(thread.name)
        people = {view.thread.name: view for view in self.comms.thread_views(
            show_stopped=True, show_archived=True)}
        messages, limited = self._recent_messages()
        def canonical(name):
            return registry.aliases.get(name, name)

        def entry(name, *, message=None, detail=""):
            name = name if is_channel_target(name) else canonical(name)
            return RelationshipEntry(
                name, "channel" if is_channel_target(name) else "thread", people.get(name),
                message.seq if message else 0, message.timestamp if message else 0, detail,
            )

        inbound, outbound = {}, {}
        for message in reversed(messages):
            if message.notice or message.membership is not None:
                continue
            detail = f"{message.sender} → {message.target}\n{message.body[:240]}"
            if canonical(message.sender) == thread.name:
                key = (canonical(message.target)
                       if not is_channel_target(message.target) else message.target)
                outbound.setdefault(key, entry(message.target, message=message, detail=detail))
            elif delivery.delivers(message):
                # Expose both the sender and the actual channel, not a fake author.
                inbound.setdefault(
                    canonical(message.sender), entry(message.sender, message=message, detail=detail)
                )
                if is_channel_target(message.target):
                    inbound.setdefault(
                        message.target, entry(message.target, message=message, detail=detail)
                    )

        orders = {"children": ThreadSort.CREATED, "collaborating": ThreadSort.LAST_ACTIVITY}
        for row in state["orders"]:
            if (canonical(row["owner"]) == thread.name and row["owner_created"] == thread.created_at
                    and row["group"] in orders):
                orders[row["group"]] = ThreadSort(row["order"])
        # Existing declaration-owned timestamp sort, shared with channel members.
        sent = (self.comms.last_sent_timestamps()
                if ThreadSort.LAST_MESSAGE in orders.values() else {})

        def ordered(entries, group):
            def key(item):
                person = item.person
                return orders[group].key(item.target,
                    person.thread.created_at if person else 0,
                    person.activity.timestamp if person else 0,
                    sent.get(item.target, 0) if person else 0)
            return tuple(sorted(entries, key=key))

        children = [entry(child.name) for child in registry.threads.values()
                    if child.parent and canonical(child.parent) == thread.name]
        collaborating = []
        for edge in self._unique_edges(edges):
            if not self._incident(edge, thread):
                continue
            other_name, other_created = self._counterpart(edge, thread)
            person = people.get(other_name)
            available = person is not None and person.thread.created_at == other_created
            collaborating.append(RelationshipEntry(
                other_name, "thread", person if available else None,
                detail=edge.note, available=available,
            ))
        return ThreadCommsSnapshot(thread.name, str(self.comms.root.resolve()), (
            RelationshipGroup("inbound", "Last inbound", tuple(inbound.values())),
            RelationshipGroup("outbound", "Last outbound", tuple(outbound.values())),
            RelationshipGroup(
                "parent", "Parent fork", (entry(thread.parent),) if thread.parent else ()
            ),
            RelationshipGroup(
                "children", "Children", ordered(children, "children"), orders["children"]
            ),
            RelationshipGroup(
                "collaborating", "Collaborating", ordered(collaborating, "collaborating"),
                orders["collaborating"],
            ),
        ), limited, len(messages))
