"""A9: immutable evidence for exactly the messages a client displays.

This is a value contract, not an assertion that fetching a page paints it. A
client acknowledges it only after paint. DM participant identity deliberately
uses ThreadIncarnation; process and turn generations do not alter provenance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping
from dataclasses import astuple, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .channel_targets import BuiltinChannel, is_channel_target
from .field_codec import FieldCodec
from .thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from .channels import Channel
    from .messages import Message
    from .registry_document import RegistrySnapshot


@dataclass(frozen=True, slots=True)
class Conversation:
    target: str = ""
    participants: tuple[ThreadIncarnation, ...] = ()

    def current(self, snapshot: RegistrySnapshot) -> bool:
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
    marker_revision: tuple[int, int, int, int] | None
    bus_identity: tuple[int, int] | None
    newest_seq: int | None
    older_unread: bool
    displayed: DisplayBasis

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

    def validate_for(
        self,
        root: Path,
        worktree: str,
        peer: str,
        through: int,
        snapshot: RegistrySnapshot,
        bus_identity: tuple[int, int] | None,
    ) -> None:
        if (
            self.root != str(root.resolve())
            or self.worktree != str(Path(worktree).resolve())
            or self.requested_peer != peer
            or self.newest_seq is None
            or self.older_unread
            or not 0 <= through <= self.newest_seq
        ):
            raise ValueError("Painted DM read does not match a contiguous displayed page.")
        info = root.stat()
        if (info.st_dev, info.st_ino) != self.root_identity:
            raise ValueError("DM root was replaced; refresh the page.")
        selected = next(
            (thread for thread in snapshot.threads.values() if not thread.role.executable), None
        )
        viewer = snapshot.threads.get(self.viewer)
        target = snapshot.threads.get(self.peer)

        def names(canonical: str) -> frozenset[str]:
            return frozenset(
                {
                    canonical,
                    *(alias for alias, owner in snapshot.aliases.items() if owner == canonical),
                }
            )

        if (
            selected is None
            or selected.name != self.viewer
            or viewer is None
            or viewer.role.executable
            or target is None
            or snapshot.aliases.get(peer, peer) != self.peer
            or viewer.incarnation != self.viewer_identity
            or target.incarnation != self.peer_identity
            or names(self.viewer) != self.viewer_names
            or names(self.peer) != self.peer_names
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

    @classmethod
    def capture(cls, a: str, b: str, snapshot: RegistrySnapshot) -> DMDisplayScope:
        def names(name: str) -> frozenset[str]:
            canonical = snapshot.aliases.get(name, name)
            return frozenset(
                {
                    canonical,
                    *(alias for alias, owner in snapshot.aliases.items() if owner == canonical),
                }
            )

        return cls(names(a), names(b))

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
        cls, channel: Channel, snapshot: RegistrySnapshot, *, seen: frozenset[int] = frozenset()
    ) -> ChannelDisplayScope:
        members = frozenset(
            name
            for name, thread in snapshot.threads.items()
            if channel.any_mode and channel.exact and channel.tags <= thread.tags
        )
        participant_names = members | frozenset(
            alias for alias, owner in snapshot.aliases.items() if owner in members
        )
        targets = channel.builtin.history_targets if channel.builtin else frozenset({channel.name})
        return cls(channel.name, targets, channel.any_mode, participant_names, seen)

    @property
    def index_targets(self) -> frozenset[str] | None:
        # The existing offset index has no mention column. Any-mode can match
        # sender, DM peer or a mention regardless of target; never drop those rows.
        return None if self.any_mode else self.targets

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
            or (
                not is_channel_target(message.target)
                and not BuiltinChannel.is_alias(message.target)
                and message.target in self.participant_names
            )
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
