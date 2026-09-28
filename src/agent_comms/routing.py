"""Routing: declaration and persistence owners."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from .channel_targets import is_channel_target
from .goals import Goal
from .messages import Message


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
    # New ordinary direct DM interrupting an active goal, not a goal attempt.
    # None distinguishes the ordinary path from all existing goal/wait turns.
    direct_interrupt_goal_id: str | None = None
    direct_interrupt_goal_revision: int | None = None
    direct_interrupt_wait_id: str | None = None
    direct_interrupt_input_key: str | None = None
    direct_interrupt_ticket: str | None = None

    def still_current_interrupt(self, goal: Goal | None) -> bool:
        """A NEW queued DM survives benign same-goal revision bumps.

        Ordinary goal progress or a standby report bumps the revision without
        changing the goal identity; that must not strand an unattempted input.
        Only a goal replacement (a different, fresh goal ID) or an inactive
        goal invalidates the queue entry. Dispatch-time admission separately
        rechecks the unattempted disposition row before any native start.
        """
        return self.direct_interrupt_goal_id is None or (
            goal is not None and goal.state.active and goal.id == self.direct_interrupt_goal_id
        )

    @property
    def reply_target(self) -> str | None:
        return self.origin.reply_target if self.origin else None

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
        """Combine compatible channel turns, keeping each direct input separate."""
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
