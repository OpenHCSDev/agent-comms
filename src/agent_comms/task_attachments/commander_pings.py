"""Committed commander notifications own what the heartbeat has already handled.

A packet carries only lines that need the commander. New lines wake it; unchanged lines are
never restated; lines still undisposed past the answer window follow the new ones and refuse
the commander's writing verbs until a recorded action on their subject disposes of them. No
process-local cache owns any of it.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.bus_publication import AgentOrigin
from agent_comms.channel_targets import ThreadTarget

from agent_comms.declared_family import DeclaredFamily
from agent_comms.commander_service import CommanderService
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import OrderId
from agent_comms.message_reference import MessageReference
from agent_comms.heartbeat_subjects import HeartbeatSubject
from agent_comms.messages import (
    ConversationRole, InfoKind, Message, MessageRole, NoticeRole,
)
from agent_comms.recorded_time import EventTime, UnrecordedEventTime
from agent_comms.standing_rules import StandingRule
from agent_comms.task_sources import TaskAttachment
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.ui_model.unit_state import UnitState

if TYPE_CHECKING:
    from agent_comms.bus import BusReader
    from agent_comms.comms import Comms
    from agent_comms.commander_hud import CommanderHud
    from agent_comms.registry_document import RegistrySnapshot


@dataclass(frozen=True)
class OrderKey:
    problem: str
    order: OrderId


@dataclass(frozen=True)
class Ping(DeclaredFamily, affix="Ping"):
    """One heartbeat line: the work it asks for, what disposes of it, and when it began.

    ``since`` is carried from the previous packet while the observation is unchanged, so a
    line keeps its age until its condition changes or a recorded action on a subject
    disposes of it.
    """

    text: str = field(compare=False, kw_only=True)
    subjects: tuple[HeartbeatSubject, ...] = field(default=(), compare=False, kw_only=True)
    since: EventTime = field(default_factory=UnrecordedEventTime, compare=False, kw_only=True)

    @property
    @abstractmethod
    def key(self) -> object: ...

    def undisposed(self, comms: Comms, actors: frozenset[str], cut: float) -> bool:
        """Began before ``cut`` and no subject has a recorded action since it began."""
        return (self.since.before(cut) and bool(self.subjects)
                and not any(subject.disposed(comms, actors, self.since.recorded_at("A line"))
                            for subject in self.subjects))

    def acted_on(self, acted: frozenset[HeartbeatSubject]) -> bool:
        return any(subject.acted_on(acted) for subject in self.subjects)

    @property
    def commander_disposes(self) -> bool:
        """Whether a commander verb disposes of this line; only such a line locks the verbs."""
        return True

    def locks(self, cut: float) -> bool:
        """Undisposed past ``cut`` and the commander's to dispose of."""
        return self.commander_disposes and self.since.before(cut)

    def render(self, now: float) -> str:
        minutes = int((now - self.since.recorded_at("A heartbeat line")) // 60)
        return f"{self.text} ({minutes} min)"


@dataclass(frozen=True)
class RulePing(Ping):
    observation: TextDigest

    @property
    def key(self) -> TextDigest:
        return self.observation

    @classmethod
    def capture(cls, rule: StandingRule) -> Self:
        return cls(rule.observation_key(), text=rule.line(), subjects=rule.subjects())


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

    @property
    def commander_disposes(self) -> bool:
        return self.state.commander_disposes

    @classmethod
    def capture(cls, state: UnitState) -> Self:
        return cls(
            state.unit, type(state),
            tuple(OrderKey(todo.id, todo.order.checked().id)
                  for todo in state.source.todos if todo.id in state.orders),
            tuple(message.reference for message in state.owed),
            state.observation_key(), text=state.line(), subjects=state.subjects(),
        )


@dataclass(frozen=True)
class CommanderUpdate(TaskAttachment, declared_name=None):
    commander: ThreadIncarnation
    handled: tuple[Ping, ...]
    not_applied: tuple[str, ...] = field(default=(), kw_only=True)
    role: ClassVar[MessageRole]

    def commander_update(self, commander: ThreadIncarnation) -> CommanderUpdate | None:
        return self if self.commander == commander else None

    @abstractmethod
    def body(self, now: float, frame: tuple[str, ...]) -> str: ...

    def undisposed(self, comms: Comms, commander: str, cut: float) -> tuple[Ping, ...]:
        """Lines older than ``cut`` that no recorded action on their subjects has disposed of."""
        actors = CommanderService.actors(comms.registry.snapshot(), commander)
        return tuple(ping for ping in self.handled if ping.undisposed(comms, actors, cut))

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
    def owed(cls, comms: Comms, commander: str, cut: float) -> tuple[Ping, ...]:
        """The newest packet's lines still undisposed past ``cut``."""
        latest = cls.latest(comms, comms.registry.require(commander).incarnation)
        return latest.undisposed(comms, commander, cut) if latest else ()

    @classmethod
    def derive(cls, commander: ThreadIncarnation, states: tuple[UnitState, ...],
               rules: tuple[StandingRule, ...],
               previous: CommanderUpdate | None, *, now: float,
               undisposed: tuple[Ping, ...] = (),
               not_applied: tuple[str, ...] = ()) -> CommanderUpdate | None:
        """New lines wake the commander and unchanged lines are never restated. With nothing new,
        cleared lines are committed by a passive notice so a recurrence is new again; with
        nothing cleared either, nothing is sent."""
        known = {ping.key: ping for ping in previous.handled} if previous else {}
        # Equality ignores the start, so an unchanged observation keeps the start it had.
        began = {ping: ping.since for ping in known.values()}
        handled = tuple(replace(ping, since=began.get(ping, ping.since).continued_from(now))
                        for ping in (
            *(UnitPing.capture(state) for state in states if state.attention()),
            *(RulePing.capture(rule) for rule in rules),
        ))
        new = tuple(ping for ping in handled if known.get(ping.key) != ping)
        if new:
            return CommanderDecisions(
                commander, handled, new,
                tuple(ping for ping in undisposed if ping in handled), not_applied=not_applied,
            )
        if previous and (handled != previous.handled or not_applied != previous.not_applied):
            return CommanderNotice(commander, handled, not_applied=not_applied)
        return None

    def send(self, comms: Comms, now: float, hud: CommanderHud) -> Message:
        from agent_comms.commander_heartbeat import CommanderHeartbeat

        message = Message(
            sender=CommanderHeartbeat.sender, target=self.commander.name,
            body=self.body(now, replace(hud, units=()).frame),
            type=InfoKind, role=self.role, task=self,
        )
        return comms.bus.publisher.publish_ordinary(
            message, origin=AgentOrigin(), destination=ThreadTarget(self.commander.name),
        )


@dataclass(frozen=True)
class CommanderNotice(CommanderUpdate):
    role = NoticeRole()

    def body(self, now: float, frame: tuple[str, ...]) -> str:
        return "\n".join((*frame, f"Lines cleared; {len(self.handled)} still current.",
                          *self.not_applied))


@dataclass(frozen=True)
class CommanderDecisions(CommanderUpdate):
    """The new lines first, then the lines still undisposed, oldest first."""

    decisions: tuple[Ping, ...]
    waiting: tuple[Ping, ...]
    role = ConversationRole()

    def body(self, now: float, frame: tuple[str, ...]) -> str:
        waiting = sorted(self.waiting, key=lambda ping: ping.since.chronology())
        return "\n".join((
            *frame, *(ping.render(now) for ping in self.decisions), *self.not_applied,
            *(("UNDISPOSED:", *(ping.render(now) for ping in waiting)) if waiting else ()),
        ))
