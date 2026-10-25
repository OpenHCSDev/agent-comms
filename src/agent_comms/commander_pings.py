"""Committed commander notifications own what the heartbeat has already handled.

Quiet state changes are passive notices. Urgent packets lead with five decisions
and publish every current observation; no process-local cache owns them.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.bus_publication import AgentOrigin
from agent_comms.channel_targets import ThreadTarget

from agent_comms.declared_family import DeclaredFamily
from agent_comms.commander_service import CommanderService
from agent_comms.errors import RelationViolationError
from agent_comms.message_reference import MessageReference
from agent_comms.messages import (
    ConversationRole, InfoKind, Message, MessageRole, NoticeRole,
)
from agent_comms.plan_phases import Plan, PlanPhase
from agent_comms.standing_rules import StandingRule
from agent_comms.task_sources import TaskAttachment
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.ui_model.unit_state import UnitState

if TYPE_CHECKING:
    from agent_comms.bus import BusReader
    from agent_comms.comms import Comms
    from agent_comms.registry_document import RegistrySnapshot


@dataclass(frozen=True)
class OrderKey:
    problem: str
    order: str


@dataclass(frozen=True)
class Ping(DeclaredFamily, affix="Ping"):
    text: str = field(compare=False, kw_only=True)

    @property
    @abstractmethod
    def key(self) -> object: ...


@dataclass(frozen=True)
class RulePing(Ping):
    observation: TextDigest

    @property
    def key(self) -> TextDigest:
        return self.observation

    @classmethod
    def capture(cls, rule: StandingRule) -> Self:
        return cls(rule.observation_key(), text=rule.line())


@dataclass(frozen=True)
class UnitPing(Ping):
    """One handled observation, not a separately writable current unit state."""
    unit: ThreadIncarnation
    state: type[UnitState]
    orders: tuple[OrderKey, ...]
    questions: tuple[MessageReference, ...]
    observation: TextDigest

    @property
    def key(self) -> ThreadIncarnation:
        return self.unit

    @classmethod
    def capture(cls, state: UnitState) -> Self:
        return cls(
            state.unit, type(state),
            tuple(OrderKey(todo.id, todo.order.checked().id)
                  for todo in state.source.todos if todo.id in state.orders),
            tuple(message.reference for message in state.owed),
            state.observation_key(), text=state.line(),
        )


@dataclass(frozen=True)
class HudPing(Ping, declared_name=None):
    """A HUD row changes when its rendered line changes, even with the same identity.

    The ping text is a change detector, not a carrier: it tells the commander a row moved.
    Views read the row itself from CommanderFrame, written from the same CommanderHud.
    """

    text: str = field(compare=True, kw_only=True)


@dataclass(frozen=True)
class UnitRowPing(HudPing):
    unit: ThreadIncarnation

    @property
    def key(self) -> tuple[type[Self], ThreadIncarnation]:
        return type(self), self.unit


@dataclass(frozen=True)
class PhaseRowPing(HudPing):
    phase: type[PlanPhase]

    @property
    def key(self) -> tuple[type[Self], type[PlanPhase]]:
        return type(self), self.phase


@dataclass(frozen=True)
class FinishedPhasesRowPing(HudPing):
    plan: type[Plan]

    @property
    def key(self) -> tuple[type[Self], type[Plan]]:
        return type(self), self.plan


@dataclass(frozen=True)
class ForksRowPing(HudPing):
    @property
    def key(self) -> tuple[type[ForksRowPing]]:
        return (ForksRowPing,)


@dataclass(frozen=True)
class LoadPing(HudPing):
    @property
    def key(self) -> tuple[type[LoadPing]]:
        return (LoadPing,)


@dataclass(frozen=True)
class CommanderUpdate(TaskAttachment, declared_name=None):
    commander: ThreadIncarnation
    handled: tuple[Ping, ...]
    not_applied: tuple[str, ...] = field(default=(), kw_only=True)
    role: ClassVar[MessageRole]

    def commander_update(self, commander: ThreadIncarnation) -> CommanderUpdate | None:
        return self if self.commander == commander else None

    @abstractmethod
    def body(self, previous: CommanderUpdate | None) -> str: ...

    def changed_pings(self, previous: CommanderUpdate | None,
                      presented: tuple[Ping, ...] = ()) -> tuple[Ping, ...]:
        known = frozenset(previous.handled) if previous else frozenset()
        return tuple(ping for ping in self.handled if ping not in known and ping not in presented)

    def changed_lines(self, previous: CommanderUpdate | None,
                      presented: tuple[Ping, ...] = ()) -> tuple[str, ...]:
        prior = previous.handled if previous else ()
        current = frozenset(ping.key for ping in self.handled)
        return (
            *(ping.text for ping in self.changed_pings(previous, presented)),
            *(f"NO LONGER CURRENT: {ping.text}" for ping in prior if ping.key not in current),
        )

    def require_publication(self, sender: str, registry: RegistrySnapshot,
                            original_source: BusReader) -> None:
        if registry.require(sender).incarnation != registry.require(CommanderService.sender).incarnation:
            raise RelationViolationError("Only the commander heartbeat publishes its observations")

    def require_target(self, target: str, registry: RegistrySnapshot) -> None:
        if registry.require(target).incarnation != self.commander:
            raise RelationViolationError("Commander observations changed recipient incarnation")

    @classmethod
    def latest(cls, comms: Comms, commander: ThreadIncarnation) -> CommanderUpdate | None:
        snapshot = comms.registry.snapshot()
        messages = comms.bus.bus.latest_addressed(
            snapshot.names_for(CommanderService.sender), snapshot.names_for(commander.name),
        )
        if not messages:
            return None
        message, = messages
        return message.task.commander_update(commander)

    @classmethod
    def derive(cls, commander: ThreadIncarnation, states: tuple[UnitState, ...],
               rules: tuple[StandingRule, ...],
               previous: CommanderUpdate | None, *,
               hud: tuple[Ping, ...],
               not_applied: tuple[str, ...] = ()) -> CommanderUpdate | None:
        known = {ping.key: ping for ping in previous.handled} if previous else {}
        quiet = (*hud, *(UnitPing.capture(state) for state in states if not state.attention()))
        urgent = (*(UnitPing.capture(state) for state in states if state.attention()),
                  *(RulePing.capture(rule) for rule in rules))
        decisions = tuple(ping for ping in urgent if known.get(ping.key) != ping)[:5]
        handled = (*quiet, *urgent)
        if decisions:
            return CommanderDecisions(commander, handled, decisions, not_applied=not_applied)
        if (previous is None or handled != previous.handled
                or not_applied != previous.not_applied):
            return CommanderNotice(commander, handled, not_applied=not_applied)
        return None

    def send(self, comms: Comms, previous: CommanderUpdate | None) -> Message:
        from agent_comms.commander_heartbeat import CommanderHeartbeat

        message = Message(
            sender=CommanderHeartbeat.sender, target=self.commander.name,
            body=self.body(previous), type=InfoKind, role=self.role, task=self,
        )
        return comms.bus.publisher.publish_ordinary(
            message, origin=AgentOrigin(), destination=ThreadTarget(self.commander.name),
        )


@dataclass(frozen=True)
class CommanderNotice(CommanderUpdate):
    role = NoticeRole()

    def body(self, previous: CommanderUpdate | None) -> str:
        return "\n".join(("Commander observations updated; no new decision.",
                          *self.not_applied,
                          *self.changed_lines(previous)))


@dataclass(frozen=True)
class CommanderDecisions(CommanderUpdate):
    decisions: tuple[Ping, ...]
    role = ConversationRole()

    def body(self, previous: CommanderUpdate | None) -> str:
        also_current = self.changed_pings(previous, self.decisions)
        return "\n".join((*(ping.text for ping in self.decisions),
                          *self.not_applied,
                          *(("ALSO CURRENT:", *(ping.text for ping in also_current))
                            if also_current else ()),
                          *self.changed_lines(previous, self.handled)))
