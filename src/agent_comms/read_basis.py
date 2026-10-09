"""A9: immutable evidence for exactly the messages a client displays.

This is a value contract, not an assertion that fetching a page paints it. A
client acknowledges it only after paint. DM participant identity deliberately
uses ThreadIncarnation; process and turn generations do not alter provenance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping
from dataclasses import astuple, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .channel_targets import is_channel_target
from .errors import UnregisteredThreadError
from .field_codec import FieldCodec
from .store_files import _store_lock
from .thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from .channels import Channel
    from .message_bus import MessageBus
    from .message_page import MessagePage
    from .messages import Message
    from .registration import Registration
    from .registry_document import RegistrySnapshot
    from .registry_provenance import RegistryProvenance


@dataclass(frozen=True, slots=True)
class Conversation:
    target: str = ""
    participants: tuple[ThreadIncarnation, ...] = ()

    def current(self, snapshot: RegistryProvenance) -> bool:
        return all(participant.current(snapshot) for participant in self.participants)

    def to_wire(self) -> dict:
        """Encode the persisted participant pair format."""
        data = FieldCodec.encode(self)
        data["participants"] = [list(astuple(participant)) for participant in self.participants]
        return data

    @classmethod
    def from_wire(cls, raw: dict) -> Conversation:
        """Decode participant pairs once at the persisted-key boundary."""
        data = dict(raw)
        data["participants"] = [
            FieldCodec.encode(ThreadIncarnation(*FieldCodec.decode(tuple[str, float], item)))
            for item in data.get("participants", [])
        ]
        return FieldCodec.decode(cls, data)


@dataclass(frozen=True, slots=True)
class DisplayedConversation:
    conversation: Conversation
    sequences: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DisplayBasis:
    viewer: str
    viewer_created_at: float
    conversations: tuple[DisplayedConversation, ...]
    bus_identity: tuple[int, int] | None

    @property
    def viewer_identity(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.viewer, self.viewer_created_at)

    def select(self, sequences: Collection[int]) -> DisplayBasis:
        """Retain painted members of this proof; never introduce new sequences."""
        selected = frozenset(sequences)
        return DisplayBasis(
            self.viewer,
            self.viewer_created_at,
            tuple(
                DisplayedConversation(
                    item.conversation, tuple(n for n in item.sequences if n in selected)
                )
                for item in self.conversations
            ),
            self.bus_identity,
        )

    def through(self, sequence: int) -> DisplayBasis:
        if sequence < 0:
            raise ValueError("Display sequence cannot be negative.")
        return DisplayBasis(
            self.viewer,
            self.viewer_created_at,
            tuple(
                DisplayedConversation(
                    item.conversation, tuple(n for n in item.sequences if n <= sequence)
                )
                for item in self.conversations
            ),
            self.bus_identity,
        )

    def validate(
        self, viewer: str, snapshot: RegistrySnapshot, bus_identity: tuple[int, int] | None
    ) -> None:
        thread = snapshot.threads.get(viewer)
        if (
            viewer != self.viewer
            or thread is None
            or thread.incarnation != self.viewer_identity
            or bus_identity != self.bus_identity
            or any(not item.conversation.current(snapshot) for item in self.conversations)
        ):
            raise ValueError("Display incarnation changed; refresh the displayed page.")


@dataclass(frozen=True, slots=True)
class DMDisplayBasis:
    """Immutable identity/scope witness for one fetched human DM page.

    A page fetch is not proof that a UI painted it. The client must supply
    separate contiguous painted-tail evidence before requesting a read mark.
    """

    root: str
    root_identity: tuple[int, int]
    worktree: str
    requested_peer: str
    viewer_identity: ThreadIncarnation
    viewer_names: frozenset[str]
    peer_identity: ThreadIncarnation
    peer_names: frozenset[str]
    bus_identity: tuple[int, int] | None
    newest_seq: int | None
    older_unread: bool
    displayed: DisplayBasis

    @classmethod
    def fetch_page(
        cls,
        bus: MessageBus,
        registry: Registration,
        root: Path,
        viewer: str,
        peer: str,
        *,
        worktree: str,
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> MessagePage:
        """Fetch a bounded human DM page and its immutable identity/read basis.

        Fetching is not paint proof. A UI may use the basis only after proving
        that the corresponding inbound tail was contiguous and visibly painted.
        """
        if not isinstance(peer, str) or is_channel_target(peer):
            raise ValueError("A DM page requires a registered peer.")
        with _store_lock(root / "wire", shared=True):
            snapshot = registry.snapshot()
            viewer_name = snapshot.canonical_name(viewer)
            peer_name = snapshot.canonical_name(peer)
            scope = DMDisplayScope.capture_human(viewer_name, peer_name, snapshot)
            viewer_thread = snapshot.threads[viewer_name]
            peer_thread = snapshot.participant(peer_name)
            viewer_names, peer_names = scope.first_names, scope.second_names
            bus_identity = bus.reads.bus_identity(bus.log.path)
        # Native admission uses this same wire lock. Page preparation and the
        # older-unread scan use opened source boundaries, outside admission.
        page = bus.display_page(
            scope, before=before, after=after, limit=limit, max_bytes=max_bytes
        )
        older_unread = False
        if page.has_older and page.messages:
            seen = bus.reads.seen_sequences(viewer_name, snapshot)
            with bus.log._record_snapshot(need_sequence=False) as (_, records):
                older_unread = any(
                    scope.unread_before(message, page.messages[0].seq, seen)
                    for message, _ in records
                )
        with _store_lock(root / "wire", shared=True), registry.store.reading() as document:
            root_info = root.stat()
            basis = cls(
                root=str(root.resolve()),
                root_identity=(root_info.st_dev, root_info.st_ino),
                worktree=str(Path(worktree).resolve()),
                requested_peer=peer,
                viewer_identity=viewer_thread.incarnation,
                viewer_names=viewer_names,
                peer_identity=peer_thread.incarnation,
                peer_names=peer_names,
                bus_identity=bus_identity,
                newest_seq=page.newest_seq,
                older_unread=older_unread,
                displayed=bus.reads.capture(viewer_name, page.messages, snapshot, bus.log.path),
            )
            basis.validate_identity(document.snapshot(), bus.reads.bus_identity(bus.log.path))
            return replace(page, display_basis=basis)

    @property
    def viewer(self) -> str:
        return self.viewer_identity.name

    @property
    def viewer_created_at(self) -> float:
        return self.viewer_identity.created_at

    @property
    def peer(self) -> str:
        return self.peer_identity.name

    @property
    def peer_created_at(self) -> float:
        return self.peer_identity.created_at

    def acknowledge(
        self,
        bus: MessageBus,
        registry: Registration,
        root: Path,
        peer: str,
        *,
        worktree: str,
        through: int,
    ) -> None:
        """CAS one painted human DM tail, never a global/executor ACK.

        The page must have no omitted unread inbound messages. The caller must
        additionally prove its through-bound was actually and contiguously
        painted; a fetched page alone cannot establish visibility.
        """
        if type(through) is not int:
            raise ValueError("Painted DM read requires a typed page basis and integer bound.")
        with _store_lock(root / "wire", shared=True), _store_lock(registry.store.path):
            snapshot = registry.store._read_unlocked().snapshot()
            self.validate_for(
                root,
                worktree,
                peer,
                through,
                snapshot,
                bus.reads.bus_identity(bus.log.path),
            )
            bus.reads.mark_displayed(self.viewer, self.displayed.through(through))

    def accepts_tail(self, through: int) -> bool:
        return (
            self.newest_seq is not None
            and not self.older_unread
            and 0 <= through <= self.newest_seq
        )

    def validate_for(
        self,
        root: Path,
        worktree: str,
        peer: str,
        through: int,
        snapshot: RegistrySnapshot,
        bus_identity: tuple[int, int] | None,
    ) -> None:
        if (self.root, self.worktree, self.requested_peer) != (
            str(root.resolve()),
            str(Path(worktree).resolve()),
            peer,
        ) or not self.accepts_tail(through):
            raise ValueError("Painted DM read does not match a contiguous displayed page.")
        info = root.stat()
        if (info.st_dev, info.st_ino) != self.root_identity:
            raise ValueError("DM root was replaced; refresh the page.")
        self.validate_identity(snapshot, bus_identity)

    def validate_identity(
        self, snapshot: RegistrySnapshot, bus_identity: tuple[int, int] | None
    ) -> None:
        """Fetch and painted acknowledgement share actual identity/scope validation.

        Heartbeat, turn state and another view's read progress are not identity.
        """
        try:
            selected = next(
                thread for thread in snapshot.threads.values() if not thread.role.executable
            )
            viewer = snapshot.threads[self.viewer]
            target = snapshot.participant(self.requested_peer)
            scope = DMDisplayScope.capture_human(self.viewer, self.requested_peer, snapshot)
        except (KeyError, StopIteration, ValueError, UnregisteredThreadError) as error:
            raise ValueError("DM viewer/peer incarnation changed; refresh the page.") from error
        if (
            selected.incarnation != self.viewer_identity
            or (viewer.incarnation, target.incarnation)
            != (self.viewer_identity, self.peer_identity)
            or scope != DMDisplayScope(self.viewer_names, self.peer_names)
        ):
            raise ValueError("DM viewer/peer incarnation changed; refresh the page.")
        if bus_identity != self.bus_identity:
            raise ValueError("DM bus was replaced; refresh the page.")
        self.displayed.validate(self.viewer, snapshot, bus_identity)


class MessageDisplayScope(ABC):
    """Captured inclusion and its sound candidate reduction for the page index."""

    @property
    @abstractmethod
    def index_targets(self) -> frozenset[str] | None:
        """None means that target alone cannot exclude a candidate."""

    @abstractmethod
    def includes(self, message: Message) -> bool:
        """Apply the captured predicate to a validated authoritative message."""


@dataclass(frozen=True, slots=True)
class DMDisplayScope(MessageDisplayScope):
    first_names: frozenset[str]
    second_names: frozenset[str]

    @staticmethod
    def names_for(name: str, snapshot: RegistryProvenance) -> frozenset[str]:
        canonical = snapshot.canonical_name(name)
        return frozenset(
            {canonical, *(alias for alias, owner in snapshot.aliases.items() if owner == canonical)}
        )

    @classmethod
    def capture(cls, a: str, b: str, snapshot: RegistryProvenance) -> DMDisplayScope:
        return cls(cls.names_for(a, snapshot), cls.names_for(b, snapshot))

    @classmethod
    def capture_human(cls, viewer: str, peer: str, snapshot: RegistrySnapshot) -> DMDisplayScope:
        try:
            first = snapshot.threads[snapshot.canonical_name(viewer)]
            second = snapshot.participant(peer)
        except (KeyError, UnregisteredThreadError) as error:
            raise ValueError("DM display identity changed; refresh the page.") from error
        if first.role.executable or first.incarnation == second.incarnation:
            raise ValueError("DM display identity changed; refresh the page.")
        return cls.capture(first.name, second.name, snapshot)

    def inbound(self, message: Message) -> bool:
        return message.sender in self.second_names and message.target in self.first_names

    def unread_before(self, message: Message, before: int, seen: frozenset[int]) -> bool:
        return message.seq < before and message.seq not in seen and self.inbound(message)

    @property
    def index_targets(self) -> frozenset[str]:
        return self.first_names | self.second_names

    def includes(self, message: Message) -> bool:
        return (message.sender in self.first_names and message.target in self.second_names) or (
            message.sender in self.second_names and message.target in self.first_names
        )


@dataclass(frozen=True, slots=True)
class ChannelDisplayScope(MessageDisplayScope):
    """One captured local display predicate; it never changes delivery or history ownership."""

    channel: str
    targets: frozenset[str] | None
    any_mode: bool = False
    participant_names: frozenset[str] = frozenset()
    seen_sequences: frozenset[int] = frozenset()
    displayed: DisplayBasis | None = field(default=None, compare=False)

    @classmethod
    def capture(
        cls, channel: Channel, snapshot: RegistryProvenance, *, seen: frozenset[int] = frozenset()
    ) -> ChannelDisplayScope:
        members = frozenset(
            name
            for name, thread in snapshot.threads.items()
            if channel.any_mode and channel.exact and channel.tags <= thread.tags
        )
        participant_names = members | frozenset(
            alias for alias, owner in snapshot.aliases.items() if owner in members
        )
        targets = channel.history_targets
        return cls(channel.name, targets, channel.any_mode, participant_names, seen)

    @property
    def index_targets(self) -> frozenset[str] | None:
        # The existing offset index has no mention column. Any-mode can match
        # sender, DM peer or a mention regardless of target; never drop those rows.
        return None if self.any_mode else self.targets

    def require_displayed(self) -> DisplayBasis:
        if self.displayed is None:
            raise ValueError("Channel display scope missing; refresh the displayed page.")
        return self.displayed

    def same_projection(self, other: ChannelDisplayScope) -> bool:
        """Read progress and unrelated store revisions do not change inclusion."""
        return (
            self.channel == other.channel
            and self.targets == other.targets
            and self.any_mode == other.any_mode
            and self.participant_names == other.participant_names
        )

    def includes(self, message: Message) -> bool:
        if self.targets is None or message.target in self.targets:
            return True
        if not self.any_mode:
            return False
        return (
            message.sender in self.participant_names
            or (not is_channel_target(message.target) and message.target in self.participant_names)
            or any(mention.thread in self.participant_names for mention in message.mentions)
        )

    def unread(self, message: Message) -> bool:
        return self.includes(message) and message.seq not in self.seen_sequences


@dataclass(frozen=True, slots=True)
class ViewUnread:
    revision: tuple | None
    scopes: tuple[ChannelDisplayScope, ...]
    counts: Mapping[str, int]
    verified_display_boundary: bool = False
