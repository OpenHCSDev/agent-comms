"""Routing: declaration and persistence owners."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .channel_targets import BuiltinChannel, is_channel_target
from .messages import Message
from .message_reference import MessageReference
from .field_codec import FieldCodec
from .read_basis import ChannelDisplayScope, DMDisplayScope, MessageDisplayScope
from .wake import derive_exact_reply_target

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot


@dataclass(frozen=True, slots=True)
class MessageRoute:
    sender: str
    targets: tuple[str, ...]

    @property
    def outgoing_label(self) -> str:
        return "To " + ", ".join(self.targets)

    @property
    def incoming_scope(self) -> str:
        return (
            ", ".join(
                target if is_channel_target(target) else "direct message" for target in self.targets
            )
            or "direct message"
        )

    @property
    def incoming_label(self) -> str:
        return f"Incoming from {self.sender}: via {self.incoming_scope}"

    @classmethod
    def from_wire(cls, data: Mapping) -> MessageRoute:
        return FieldCodec.decode(cls, data)


@dataclass(frozen=True, slots=True)
class TurnRouting:
    requests: tuple[MessageReference, ...] = ()
    reply: MessageRoute | None = None
    publications: tuple[MessageReference, ...] = field(
        default=(), metadata={"wire_omit_default": True}
    )

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode(self)

    @classmethod
    def from_wire(cls, data: Mapping) -> TurnRouting:
        return FieldCodec.decode(cls, data)

    def requires_annotation(self, published: tuple[MessageReference, ...]) -> bool:
        return bool(self.requests or published)

    def annotation_ids(self, reader, checkpoint, session_file: str, through: int):
        return (reader.annotation_ids(checkpoint.offset, through)
                if self.requests and session_file == checkpoint.session_file else ())

    def require_publications(self, originals, owner_lookup: str) -> None:
        """Only committed originals addressed by this reply intent can be joined."""
        from .errors import RelationViolationError

        if self.publications:
            raise RelationViolationError("Turn intent cannot preclaim a publication")
        if not originals:
            return
        if self.reply is None or len(originals) > len(self.reply.targets):
            raise RelationViolationError("Publication exceeds this turn's reply intent")
        for original in originals:
            if original.audience.sender_lookup != owner_lookup:
                raise RelationViolationError("Publication belongs to another original sender")
            if original.message.target not in self.reply.targets:
                raise RelationViolationError("Publication is outside this turn's reply targets")


@dataclass(frozen=True, slots=True)
class ScheduledTurn:
    prompt: str
    origin: Message | None = None
    goal_id: str | None = None
    goal_wait_id: str | None = None

    @property
    def autonomous_goal(self) -> bool:
        return self.goal_id is not None and self.origin is None

    def require_dependency_wake(self, error: Exception) -> None:
        """Only dependency launches use standby refusal settlement."""
        if self.goal_wait_id is None:
            raise error

    @property
    def origins(self) -> tuple[Message, ...]:
        return (self.origin,) if self.origin is not None else ()

    @classmethod
    def incoming(
        cls, message: Message, *, aliases: Mapping[str, str] | None = None
    ) -> ScheduledTurn:
        guidance = message.response_policy.guidance(message, aliases=aliases)
        scope = (
            f"; delivery and history remain {message.target}"
            if is_channel_target(message.target)
            else ""
        )
        return cls(
            f"[agent-comms from {message.sender} to {message.target}]\n"
            f"[Response policy: {guidance}{scope}]\n{message.body}",
            origin=message,
        )

    @staticmethod
    def take_batch(pending: list[ScheduledTurn]) -> tuple[list[ScheduledTurn], list[ScheduledTurn]]:
        """Combine channel turns sharing a route, keeping each direct input separate."""
        if not pending:
            return [], []
        if pending[0].origin and pending[0].origin.response_policy.separate_turn:
            return pending[:1], pending[1:]
        boundary = next(
            (
                index
                for index, turn in enumerate(pending)
                if derive_exact_reply_target(turn.origin)
                != derive_exact_reply_target(pending[0].origin)
                or (turn.origin and turn.origin.response_policy.separate_turn)
            ),
            len(pending),
        )
        return pending[:boundary], pending[boundary:]


@dataclass(frozen=True, slots=True)
class DeliveryMessage:
    """The current Message decoder plus its existing publication identity."""

    message: Message
    sender_lookup: str = ""

    @classmethod
    def from_wire(cls, record: Mapping, root_id: str | None):
        from .wire_record import WireRecord

        verified = WireRecord.from_wire(record, root_id)
        yield from verified.delivery_messages()



@dataclass(frozen=True, slots=True)
class DeliveryScope(MessageDisplayScope):
    actor: str
    aliases: Mapping[str, str]
    channels: frozenset[str]

    @property
    def index_targets(self) -> frozenset[str]:
        return self.channels | frozenset(
            name for name in (self.actor, *self.aliases) if self.canonical(name) == self.actor
        )

    def includes(self, message: Message) -> bool:
        return self.delivers(message.sender, message.target)

    def selection(self, target, snapshot, catalog) -> MessageDisplayScope:
        if target is None:
            return ChannelDisplayScope(BuiltinChannel.ANY.value, None)
        if is_channel_target(target):
            return ChannelDisplayScope(target, catalog.history_targets(target))
        peer = snapshot.aliases.get(target, target)
        if peer not in snapshot.threads:
            from .errors import UnregisteredThreadError

            raise UnregisteredThreadError(f"Thread {target!r} is not registered.")
        return DMDisplayScope.capture(self.actor, peer, snapshot)

    def canonical(self, name: str) -> str:
        return self.aliases.get(name, name)

    def delivers(self, sender: str, target: str) -> bool:
        return self.canonical(sender) != self.actor and (
            target in self.channels or self.canonical(target) == self.actor
        )

    def minimum_timestamp(
        self, sender: str, target: str, sender_lookup: str, snapshot: RegistrySnapshot
    ) -> float | None:
        """Require current identities while preserving aliases of one incarnation."""
        from .bus_publication import stable_thread_lookup

        if not self.delivers(sender, target):
            return None
        source = snapshot.threads.get(self.canonical(sender))
        if source is None:
            return None
        if sender_lookup and sender_lookup != stable_thread_lookup(source.created_at):
            return None
        return max(source.created_at, snapshot.threads[self.actor].created_at)

    def current(self, delivery: DeliveryMessage, snapshot: RegistrySnapshot) -> bool:
        message = delivery.message
        since = self.minimum_timestamp(
            message.sender, message.target, delivery.sender_lookup, snapshot
        )
        return since is not None and message.timestamp >= since

    def conversation(self, sender: str, target: str) -> str:
        if is_channel_target(target):
            return target
        sender, target = self.canonical(sender), self.canonical(target)
        return sender if target == self.actor else target


@dataclass(frozen=True, slots=True)
class PendingCounts:
    revision: tuple[tuple[int, int, int, int] | None, ...]
    delivery: DeliveryScope
    counts: Mapping[str, int]
