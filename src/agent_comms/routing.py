"""Routing: declaration and persistence owners."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from .channel_targets import is_channel_target
from .messages import Message

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
        return cls(str(data["sender"]), tuple(str(target) for target in data["targets"]))


@dataclass(frozen=True, slots=True)
class TurnRouting:
    requests: tuple[Message, ...] = ()
    reply: MessageRoute | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "requests": [message.to_wire() for message in self.requests],
            "reply": asdict(self.reply) if self.reply else None,
        }

    @classmethod
    def from_wire(cls, data: Mapping) -> TurnRouting:
        return cls(
            tuple(Message.from_wire(item) for item in data.get("requests", [])),
            MessageRoute.from_wire(data["reply"]) if data.get("reply") else None,
        )


@dataclass(frozen=True, slots=True)
class ScheduledTurn:
    prompt: str
    origin: Message | None = None
    goal_id: str | None = None
    goal_wait_id: str | None = None

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
                if turn.reply_target != pending[0].reply_target
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
    def from_wire(cls, record: Mapping, root_id: str | None) -> DeliveryMessage:
        from .bus_publication import PRIVATE_WIRE_FIELD, validate_initial_record

        private = record.get(PRIVATE_WIRE_FIELD, {})
        if "initial" in private:
            initial = validate_initial_record(record, root_id)
            return cls(initial.message, initial.audience.sender_lookup)
        return cls(Message.from_wire(record))


@dataclass(frozen=True, slots=True)
class DeliveryScope:
    actor: str
    aliases: Mapping[str, str]
    channels: frozenset[str]

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
