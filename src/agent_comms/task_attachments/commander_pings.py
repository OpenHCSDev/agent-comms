"""Committed commander notifications own the heartbeat's retained observations.

New observations and every still-undisposed request wake on each beat. A changed quiet frame
publishes once without waking; an unchanged quiet frame adds nothing to bus history. The
original subject actions dispose of requests, and the answer window only gates overdue
writing verbs. No process-local cache owns any of it.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from agent_comms.bus_publication import AgentOrigin
from agent_comms.channel_targets import ThreadTarget

from agent_comms.declared_family import DeclaredFamily
from agent_comms.harness_delivery_states import DeliveredOutcome, Outcome
from agent_comms.commander_service import CommanderService
from agent_comms.errors import RelationViolationError
from agent_comms.disclosure_text import AccentTone, ViewHeading, ViewTable, WarningTone
from agent_comms.field_codec import FieldCodec, OrderId, PublicationExclude
from agent_comms.message_reference import MessageReference
from agent_comms.heartbeat_subjects import CommanderHistory, HeartbeatSubject
from agent_comms.messages import (
    CommanderUpdatePosition, ConversationRole, InfoKind, Message, MessageRole, NoticeRole,
)
from agent_comms.projection_source import ProducedRow
from agent_comms.recorded_time import EventTime, UnrecordedEventTime
from agent_comms.standing_rules import ServicePassFailing, StandingRule
from agent_comms.task_sources import TaskAttachment
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.ui_model.unit_state import AwaitingCommander, UnitState
from agent_comms_hooks.board import BoardEntry

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agent_comms.bus import BusReader
    from agent_comms.cli_bridges.shared import CliBridge
    from agent_comms.comms import Comms
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.typed_table import TypedTable


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
    def urgent(self) -> bool:
        return False

    @property
    @abstractmethod
    def key(self) -> object: ...

    @property
    @abstractmethod
    def alarm_kind(self) -> type[DeclaredFamily]: ...

    def acted(self, history: CommanderHistory) -> bool:
        """Whether a subject has a recorded action since this line began."""
        began = self.since.recorded_at("A line")
        return any(subject.disposed(history, began) for subject in self.subjects)

    def undisposed(self, history: CommanderHistory, cut: float) -> bool:
        """Waiting on the commander, and not acted on since it began. Only such a line is
        restated as waiting and locks the verbs."""
        return self.waits_on_commander(cut) and not self.acted(history)

    def waits_on_commander(self, cut: float) -> bool:
        """The commander's to dispose of, began before ``cut``, and about a subject a verb can
        act on: a line about nothing a verb touches never waits."""
        return self.commander_disposes and self.since.before(cut)

    def on_board(self, history: CommanderHistory) -> bool:
        """The commander's to dispose of, and no recorded action has yet."""
        return self.commander_disposes and not self.acted(history)

    @property
    @abstractmethod
    def commander_disposes(self) -> bool:
        """Whether a commander verb disposes of this line; only such a line goes on the board."""

    def board_entry(self) -> BoardEntry:
        """Content uses precisely this dataclass's equality, never its rendered text or age."""
        ignored = {key for declared, key in FieldCodec.record_fields(type(self)) if not declared.compare}
        content = TextDigest.of(FieldCodec.compact({
            key: value for key, value in FieldCodec.encode(self).items() if key not in ignored
        }))
        since = self.since.recorded_at("A heartbeat line")
        return BoardEntry(content.value, self.text, since, since + AwaitingCommander.seconds)

    def render(self, now: float) -> str:
        return f"{self.text} ({self.since.age(now)})"

    def alarm_pings(self) -> tuple[Ping, ...]:
        return (self,)


@dataclass(frozen=True)
class RulePing(Ping):
    rule: type[StandingRule]
    observation: TextDigest

    @property
    def urgent(self) -> bool:
        return self.rule.urgent

    @property
    def alarm_kind(self) -> type[StandingRule]:
        return self.rule

    @property
    def key(self) -> TextDigest:
        return self.observation

    @property
    def commander_disposes(self) -> bool:
        return self.rule.disposes_subjects(self.subjects)

    def alarm_pings(self) -> tuple[Ping, ...]:
        return self.rule.alarm_pings(self)

    @classmethod
    def capture(cls, rule: StandingRule) -> Self:
        return cls(type(rule), rule.observation_key(), text=rule.line(), subjects=rule.subjects())


@dataclass(frozen=True)
class UnitPing(Ping):
    """One handled observation, not a separately writable current unit state."""
    unit: ThreadIncarnation
    state: type[UnitState]
    orders: tuple[OrderKey, ...]
    questions: tuple[MessageReference, ...]
    observation: TextDigest

    @property
    def urgent(self) -> bool:
        return self.state.hud_group.needs_hand()

    @property
    def alarm_kind(self) -> type[UnitState]:
        return self.state

    @property
    def key(self) -> ThreadIncarnation:
        return self.unit

    @property
    def commander_disposes(self) -> bool:
        return self.state.disposes_subjects(self.subjects)

    def alarm_pings(self) -> tuple[Ping, ...]:
        """The unit's row already carries this observation and its next action."""
        return ()

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
class CommanderDelivery:
    """A derived packet and the one open set shared by publication and its disposable HUD."""

    packet: CommanderUpdate
    open_pings: tuple[Ping, ...]


@dataclass(frozen=True)
class CommanderUpdate(TaskAttachment, declared_name=None):
    commander: ThreadIncarnation
    handled: tuple[Ping, ...]
    not_applied: tuple[str, ...] = field(default=(), kw_only=True)
    role: ClassVar[MessageRole]

    writes: ClassVar[tuple[type[ProducedRow], ...]] = (CommanderUpdatePosition,)

    def projections(self, message: Message) -> Iterator[TypedTable]:
        """The packet's position under the commander birth it was published for."""
        yield CommanderUpdatePosition(message.seq, self.commander.require_recorded().lookup)

    def commander_update(self, commander: ThreadIncarnation) -> tuple[CommanderUpdate, ...]:
        return (self,) if self.commander == commander else ()

    def should_publish(self, frame_changed: bool) -> bool:
        return True

    def new_pings(self) -> tuple[Ping, ...]:
        return ()

    def body(self, hud: tuple[str, ...]) -> str:
        """The existing HUD owns presentation; this packet owns publication and history."""
        return "\n".join((
            *hud, "",
            "Full evidence: comms_hud board/open_pings, or Toad /commander original details",
        ))

    def board(self, comms: Comms, commander: str) -> tuple[BoardEntry, ...]:
        """This packet's lines the commander disposes of, through the same recorded actions the
        gate reads: a verdict drops its line on the next read, not the next packet."""
        actors = CommanderService.actors(comms.registry.snapshot(), commander)
        history = CommanderHistory.read(comms, actors)
        return tuple(ping.board_entry() for ping in self.open_pings(history))

    def open_pings(self, history: CommanderHistory) -> tuple[Ping, ...]:
        return tuple(ping for ping in self.handled if ping.on_board(history))

    async def delivered_by(self, bridge: CliBridge, message: Message) -> type[Outcome]:
        return await bridge.deliver_board(self, message)

    @abstractmethod
    async def wake(self, bridge: CliBridge, message: Message) -> type[Outcome]:
        """The packet's wake decision is the same on every harness."""

    def undisposed(self, history: CommanderHistory, cut: float) -> tuple[Ping, ...]:
        """Read every subject through this pass's one captured actor/history reader."""
        return tuple(ping for ping in self.handled if ping.undisposed(history, cut))

    def require_publication(self, sender: str, registry: RegistrySnapshot,
                            original_source: BusReader) -> None:
        if registry.require(sender).incarnation != registry.require(CommanderService.sender).incarnation:
            raise RelationViolationError("Only the commander heartbeat publishes its observations")

    def require_target(self, target: str, registry: RegistrySnapshot) -> None:
        if registry.require(target).incarnation != self.commander:
            raise RelationViolationError("Commander observations changed recipient incarnation")

    @classmethod
    def latest(cls, comms: Comms, commander: ThreadIncarnation) -> tuple[CommanderUpdate, ...]:
        """The newest typed packet for that commander birth, or none. An independent message
        to the commander, newer or not, is never this board's baseline."""
        with comms.bus.bus.observing() as db:
            messages = CommanderUpdatePosition.latest(db, commander.require_recorded().lookup)
        return tuple(packet for message in messages
                     for packet in message.task.commander_update(commander))

    @classmethod
    def owed(cls, comms: Comms, commander: str, cut: float) -> tuple[Ping, ...]:
        """The newest packet's lines still undisposed past ``cut``."""
        history = CommanderHistory.read(comms, CommanderService.actors(comms.registry.snapshot(), commander))
        return tuple(ping for latest in cls.latest(comms, comms.registry.require(commander).incarnation)
                     for ping in latest.undisposed(history, cut))

    @classmethod
    def current_board(cls, comms: Comms, commander: str) -> tuple[BoardEntry, ...]:
        """The newest packet's board, derived at the moment of asking."""
        return tuple(entry for latest in cls.latest(comms, comms.registry.require(commander).incarnation)
                     for entry in latest.board(comms, commander))

    @classmethod
    def derive(cls, commander: ThreadIncarnation, states: tuple[UnitState, ...],
               rules: tuple[StandingRule, ...],
               previous: tuple[CommanderUpdate, ...], *, now: float,
               history: CommanderHistory, undisposed: tuple[Ping, ...] = (),
               not_applied: tuple[str, ...] = ()) -> tuple[CommanderDelivery, ...]:
        """New observations or any open request wake on every beat. Publication never disposes
        of a ping; only a beat without unresolved work may be passive."""
        known = {ping.key: ping for packet in previous for ping in packet.handled}
        # Equality ignores the start, so an unchanged observation keeps the start it had.
        began = {ping: ping.since for ping in known.values()}
        handled = tuple(sorted(
            (replace(ping, since=began.get(ping, ping.since).continued_from(now))
             for ping in (
                 *(UnitPing.capture(state) for state in states if state.attention()),
                 *(RulePing.capture(rule) for rule in rules),
             )), key=lambda ping: not ping.urgent,
        ))
        new = tuple(ping for ping in handled if known.get(ping.key) != ping)
        notice = CommanderNotice(commander, handled, not_applied=not_applied)
        opened = notice.open_pings(history)
        packet = CommanderDecisions(
            commander, handled, new,
            tuple(ping for ping in undisposed if ping in handled), not_applied=not_applied,
        ) if new or opened or not_applied else notice
        return (CommanderDelivery(packet, opened),)

    @classmethod
    def report_failure(cls, commander: ThreadIncarnation, failure: ServicePassFailing,
                       previous: tuple[CommanderUpdate, ...], *,
                       now: float) -> tuple[CommanderUpdate, ...]:
        """A failed pass read no board, so it clears nothing: it adds only its own retained
        occurrence, once. Every handled line keeps its object and age for the next complete
        read to derive from."""
        ping = RulePing.capture(failure)
        handled = tuple(line for packet in previous for line in packet.handled)
        if ping in handled:
            return ()
        ping = replace(ping, since=ping.since.continued_from(now))
        return (CommanderDecisions(
            commander, (*handled, ping), (ping,), (),
            not_applied=tuple(line for packet in previous for line in packet.not_applied),
        ),)

    def send(self, comms: Comms, now: float, hud: tuple[str, ...]) -> Message:
        from agent_comms.commander_heartbeat import CommanderHeartbeat

        message = Message(
            sender=CommanderHeartbeat.sender, target=self.commander.name,
            body=self.body(hud),
            type=InfoKind, role=self.role, task=self,
        )
        return comms.bus.publisher.publish_ordinary(
            message, origin=AgentOrigin(), destination=ThreadTarget(self.commander.name),
        )


@dataclass(frozen=True)
class CommanderNotice(CommanderUpdate):
    """Publish a changed quiet frame once, without waking for an identical copy."""

    role = NoticeRole()

    def should_publish(self, frame_changed: bool) -> bool:
        return frame_changed

    async def wake(self, bridge: CliBridge, message: Message) -> type[Outcome]:
        """The current display has no unresolved request that needs model input."""
        return DeliveredOutcome


@dataclass(frozen=True)
class CommanderDecisions(CommanderUpdate):
    """The new lines first, then the lines still undisposed, oldest first."""

    decisions: tuple[Ping, ...]
    waiting: tuple[Ping, ...]
    role = ConversationRole()

    async def wake(self, bridge: CliBridge, message: Message) -> type[Outcome]:
        return await bridge.wake(message)

    def new_pings(self) -> tuple[Ping, ...]:
        return self.decisions


class AlarmGroup(ViewHeading, affix="Alarms"):
    """Alarm membership shares heading behavior, not the unit-group registry."""

    @classmethod
    @abstractmethod
    def members(cls, packets: tuple[CommanderUpdate, ...],
                opened: tuple[Ping, ...]) -> tuple[Ping, ...]: ...

    @classmethod
    def visible(cls, pings: tuple[Ping, ...]) -> tuple[Ping, ...]:
        return pings

    @classmethod
    def sections(cls, packets: tuple[CommanderUpdate, ...],
                 opened: tuple[Ping, ...]) -> tuple[AlarmSection, ...]:
        return tuple(AlarmSection(group, members)
                     for group in cls.members_with(cls)
                     for members in (group.members(packets, opened),) if members)


class CompactAlarms:
    """Sections keep actionable representatives and explicit per-kind overflow."""

    maximum_per_kind: ClassVar[int] = 1

    @classmethod
    def visible(cls, pings: tuple[Ping, ...]) -> tuple[Ping, ...]:
        return pings[:cls.maximum_per_kind]


class NewAlarms(CompactAlarms, AlarmGroup):
    tone = AccentTone

    @classmethod
    def members(cls, packets: tuple[CommanderUpdate, ...],
                opened: tuple[Ping, ...]) -> tuple[Ping, ...]:
        return tuple(line for packet in packets for ping in packet.new_pings()
                     for line in ping.alarm_pings())


class StillOpenAlarms(CompactAlarms, AlarmGroup):
    tone = WarningTone

    @classmethod
    def members(cls, packets: tuple[CommanderUpdate, ...],
                opened: tuple[Ping, ...]) -> tuple[Ping, ...]:
        new = frozenset(NewAlarms.members(packets, opened))
        return tuple(line for ping in opened for line in ping.alarm_pings() if line not in new)


class WatchingAlarms(CompactAlarms, AlarmGroup):
    """A still-failing source remains visible, but it has no commander disposal to wake for."""

    @classmethod
    def members(cls, packets: tuple[CommanderUpdate, ...],
                opened: tuple[Ping, ...]) -> tuple[Ping, ...]:
        new = frozenset(NewAlarms.members(packets, opened))
        return tuple(line for packet in packets for ping in packet.handled
                     if not ping.commander_disposes for line in ping.alarm_pings() if line not in new)


@dataclass(frozen=True)
class AlarmLine:
    kind: type[DeclaredFamily]
    line: str
    age: Annotated[str, PublicationExclude()]


@dataclass(frozen=True)
class AlarmOverflow:
    kind: type[DeclaredFamily]
    more: int
    oldest_age: Annotated[str, PublicationExclude()]


@dataclass(frozen=True)
class AlarmSection:
    group: type[AlarmGroup]
    pings: tuple[Ping, ...]

    def heading(self) -> str:
        return self.group.count_heading(len(self.pings))

    def tables(self, now: float) -> tuple[ViewTable[AlarmLine] | ViewTable[AlarmOverflow], ...]:
        grouped: dict[type[DeclaredFamily], list[Ping]] = {}
        for ping in self.pings:
            grouped.setdefault(ping.alarm_kind, []).append(ping)
        lines: list[AlarmLine] = []
        overflow: list[AlarmOverflow] = []
        for kind, members in grouped.items():
            shown = self.group.visible(tuple(members))
            lines.extend(AlarmLine(kind, ping.text, ping.since.age(now)) for ping in shown)
            rest = members[len(shown):]
            if rest:
                overflow.append(AlarmOverflow(kind, len(rest), min(
                    rest, key=lambda ping: ping.since.chronology()).since.age(now)))
        tables: list[ViewTable[AlarmLine] | ViewTable[AlarmOverflow]] = [
            ViewTable(AlarmLine, "hud", tuple(lines)),
        ]
        if overflow:
            tables.append(ViewTable(AlarmOverflow, "hud", tuple(overflow)))
        return tuple(tables)
