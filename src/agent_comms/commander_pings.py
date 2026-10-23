"""Committed commander notifications own what the heartbeat has already handled.

Quiet state changes are passive notices. Unhandled urgent states remain eligible
for the next five-decision packet; no process-local cache or pending queue owns them.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.errors import RelationViolationError
from agent_comms.message_reference import MessageReference
from agent_comms.messages import (
    ConversationRole, InfoKind, Message, MessageRole, NoticeRole,
)
from agent_comms.task_sources import TaskAttachment
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.ui_model.unit_state import UnitState

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.registry_document import RegistrySnapshot


@dataclass(frozen=True)
class OrderKey:
    problem: str
    order: str


@dataclass(frozen=True)
class UnitPing:
    """One handled observation, not a separately writable current unit state."""
    unit: ThreadIncarnation
    state: type[UnitState]
    orders: tuple[OrderKey, ...]
    questions: tuple[MessageReference, ...]
    text: str = field(compare=False)

    @classmethod
    def capture(cls, state: UnitState) -> Self:
        return cls(
            state.unit, type(state),
            tuple(OrderKey(todo.id, todo.order.checked().id)
                  for todo in state.source.todos if todo.id in state.orders),
            tuple(message.reference for message in state.owed),
            state.line(),
        )


@dataclass(frozen=True)
class CommanderUpdate(TaskAttachment, declared_name=None):
    commander: ThreadIncarnation
    handled: tuple[UnitPing, ...]
    role: ClassVar[MessageRole]

    @abstractmethod
    def body(self) -> str: ...

    def require_sender(self, sender: str) -> None:
        from agent_comms.commander_heartbeat import CommanderHeartbeat

        if sender != CommanderHeartbeat.sender:
            raise RelationViolationError("Only the commander heartbeat publishes its observations")

    def require_target(self, target: str, registry: RegistrySnapshot) -> None:
        if registry.require(target).incarnation != self.commander:
            raise RelationViolationError("Commander observations changed recipient incarnation")

    @classmethod
    def latest(cls, comms: Comms, commander: ThreadIncarnation) -> CommanderUpdate | None:
        from agent_comms.commander_heartbeat import CommanderHeartbeat

        messages = comms.bus.bus.latest_addressed(
            frozenset((CommanderHeartbeat.sender,)), frozenset((commander.name,)),
        )
        if not messages:
            return None
        message, = messages
        # Ordinary messages carry no commander-observation evidence.
        update = message.task
        if not isinstance(update, CommanderUpdate):
            return None
        return update if update.commander == commander else None

    @classmethod
    def derive(cls, commander: ThreadIncarnation, states: tuple[UnitState, ...],
               previous: CommanderUpdate | None) -> CommanderUpdate | None:
        known = {ping.unit: ping for ping in previous.handled} if previous else {}
        current = tuple(UnitPing.capture(state) for state in states)
        decisions = tuple(ping for state, ping in zip(states, current, strict=True)
                          if state.attention() and known.get(ping.unit) != ping)[:5]
        handled = tuple(
            ping if not state.attention() or ping in decisions else known[ping.unit]
            for state, ping in zip(states, current, strict=True)
            if not state.attention() or ping in decisions or ping.unit in known
        )
        if decisions:
            return CommanderDecisions(commander, handled, decisions)
        if previous is None or handled != previous.handled:
            return CommanderNotice(commander, handled)
        return None

    def send(self, comms: Comms) -> Message:
        from agent_comms.bus_publication import AgentOrigin
        from agent_comms.channel_targets import ThreadTarget
        from agent_comms.commander_heartbeat import CommanderHeartbeat

        message = Message(
            sender=CommanderHeartbeat.sender, target=self.commander.name,
            body=self.body(), type=InfoKind, role=self.role, task=self,
        )
        return comms.bus.publisher.publish_ordinary(
            message, origin=AgentOrigin(), destination=ThreadTarget(self.commander.name),
        )


@dataclass(frozen=True)
class CommanderNotice(CommanderUpdate):
    role = NoticeRole()

    def body(self) -> str:
        return "Commander observations updated; no new decision."


@dataclass(frozen=True)
class CommanderDecisions(CommanderUpdate):
    decisions: tuple[UnitPing, ...]
    role = ConversationRole()

    def body(self) -> str:
        return "\n".join(ping.text for ping in self.decisions)
