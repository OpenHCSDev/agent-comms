"""Committed commander notifications own what the heartbeat has already handled.

A packet carries only lines that need the commander. New lines wake it; unchanged lines are
never restated; lines still undisposed past the answer window follow the new ones and refuse
the commander's writing verbs until a recorded action on their subject disposes of them. No
process-local cache owns any of it.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from textwrap import shorten
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.bus_publication import AgentOrigin
from agent_comms.channel_targets import ThreadTarget

from agent_comms.declared_family import DeclaredFamily
from agent_comms.harness_delivery_states import FilteredOutcome, Outcome
from agent_comms.commander_service import CommanderService
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, OrderId
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
    from agent_comms.cli_bridges.claude import ClaudeBridge
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
    @abstractmethod
    def group(self) -> str:
        """Presentation grouping derived from the original rule or unit declaration."""

    @property
    def urgent(self) -> bool:
        return False

    @property
    @abstractmethod
    def key(self) -> object: ...

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
        return self.board_entry().aged(now)

    def preview(self, now: float) -> str:
        """A bounded text view; the board and packet retain the complete original."""
        return replace(self.board_entry(), text=shorten(self.text, width=180, placeholder="…")).aged(now)


@dataclass(frozen=True)
class RulePing(Ping):
    rule: type[StandingRule]
    observation: TextDigest

    @property
    def group(self) -> str:
        return self.rule.declared_name

    @property
    def urgent(self) -> bool:
        return self.rule.urgent

    @property
    def key(self) -> TextDigest:
        return self.observation

    @property
    def commander_disposes(self) -> bool:
        return self.rule.disposes_subjects(self.subjects)

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
    def group(self) -> str:
        return self.unit.name

    @property
    def key(self) -> ThreadIncarnation:
        return self.unit

    @property
    def commander_disposes(self) -> bool:
        return self.state.disposes_subjects(self.subjects)

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
    header: ClassVar[str] = "COMMANDER"

    writes: ClassVar[tuple[type[ProducedRow], ...]] = (CommanderUpdatePosition,)

    def projections(self, message: Message) -> Iterator[TypedTable]:
        """The packet's position under the commander birth it was published for."""
        yield CommanderUpdatePosition(message.seq, self.commander.require_recorded().lookup)

    def commander_update(self, commander: ThreadIncarnation) -> tuple[CommanderUpdate, ...]:
        return (self,) if self.commander == commander else ()

    def changed_from(self, handled: tuple[Ping, ...], not_applied: tuple[str, ...]) -> bool:
        """A complete read that differs from this packet clears what it no longer holds."""
        return (self.handled, self.not_applied) != (handled, not_applied)

    @abstractmethod
    def lines(self, now: float) -> tuple[str, ...]:
        """What this packet asks of the commander."""

    def body(self, now: float, brief: tuple[str, ...]) -> str:
        """Scan first; original evidence remains in the same packet and expandable HUD."""
        stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%H:%M:%S UTC")
        return "\n".join((
            f"{self.header} · {self.commander.name} · {stamp} · {len(self.handled)} current",
            *brief, "", *self.lines(now), "",
            "Expand in Toad: F2 or /commander · units: original orders, story, commits, activity · board: every alert",
        ))

    def board(self, comms: Comms, commander: str) -> tuple[BoardEntry, ...]:
        """This packet's lines the commander disposes of, through the same recorded actions the
        gate reads: a verdict drops its line on the next read, not the next packet."""
        actors = CommanderService.actors(comms.registry.snapshot(), commander)
        history = CommanderHistory.read(comms, actors)
        return tuple(ping.board_entry() for ping in self.handled
                     if ping.on_board(history))

    async def delivered_by(self, bridge: CliBridge, message: Message) -> type[Outcome]:
        return await bridge.deliver_board(self, message)

    @abstractmethod
    async def wake(self, bridge: ClaudeBridge, message: Message) -> type[Outcome]:
        """What reaches the session itself once its bridge has put this packet on its board."""

    def undisposed(self, comms: Comms, commander: str, cut: float) -> tuple[Ping, ...]:
        """Lines older than ``cut`` that no recorded action on their subjects has disposed of."""
        actors = CommanderService.actors(comms.registry.snapshot(), commander)
        history = CommanderHistory.read(comms, actors)
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
        return tuple(ping for latest in cls.latest(comms, comms.registry.require(commander).incarnation)
                     for ping in latest.undisposed(comms, commander, cut))

    @classmethod
    def current_board(cls, comms: Comms, commander: str) -> tuple[BoardEntry, ...]:
        """The newest packet's board, derived at the moment of asking."""
        return tuple(entry for latest in cls.latest(comms, comms.registry.require(commander).incarnation)
                     for entry in latest.board(comms, commander))

    @classmethod
    def derive(cls, commander: ThreadIncarnation, states: tuple[UnitState, ...],
               rules: tuple[StandingRule, ...],
               previous: tuple[CommanderUpdate, ...], *, now: float,
               undisposed: tuple[Ping, ...] = (),
               not_applied: tuple[str, ...] = ()) -> tuple[CommanderUpdate, ...]:
        """New lines wake the commander and unchanged lines are never restated. With nothing new,
        cleared lines are committed by a passive notice so a recurrence is new again; with
        nothing cleared either, nothing is sent."""
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
        if new:
            return (CommanderDecisions(
                commander, handled, new,
                tuple(ping for ping in undisposed if ping in handled), not_applied=not_applied,
            ),)
        return tuple(CommanderNotice(commander, handled, not_applied=not_applied)
                     for packet in previous if packet.changed_from(handled, not_applied))

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

    def send(self, comms: Comms, now: float, brief: tuple[str, ...]) -> Message:
        from agent_comms.commander_heartbeat import CommanderHeartbeat

        message = Message(
            sender=CommanderHeartbeat.sender, target=self.commander.name,
            body=self.body(now, brief),
            type=InfoKind, role=self.role, task=self,
        )
        return comms.bus.publisher.publish_ordinary(
            message, origin=AgentOrigin(), destination=ThreadTarget(self.commander.name),
        )


@dataclass(frozen=True)
class CommanderNotice(CommanderUpdate):
    role = NoticeRole()

    async def wake(self, bridge: ClaudeBridge, message: Message) -> type[Outcome]:
        """Cleared lines leave the board; nothing new needs the session."""
        return FilteredOutcome

    def lines(self, now: float) -> tuple[str, ...]:
        return (f"Cleared · {len(self.handled)} requests remain on the board.", *self.not_applied)


@dataclass(frozen=True)
class CommanderDecisions(CommanderUpdate):
    """The new lines first, then the lines still undisposed, oldest first."""

    decisions: tuple[Ping, ...]
    waiting: tuple[Ping, ...]
    role = ConversationRole()

    async def wake(self, bridge: ClaudeBridge, message: Message) -> type[Outcome]:
        return await bridge.wake(message)

    def lines(self, now: float) -> tuple[str, ...]:
        waiting = sorted(self.waiting, key=lambda ping: ping.since.chronology())
        groups: dict[str, list[Ping]] = {}
        for ping in waiting:
            groups.setdefault(ping.group, []).append(ping)
        return (
            f"New · {len(self.decisions)}",
            *(f"• {ping.preview(now)}" for ping in self.decisions), *self.not_applied,
            *(line for name, pings in groups.items() for line in (
                f"Waiting · {name} · {len(pings)}",
                *(f"• {ping.preview(now)}" for ping in pings[:2]),
                *((f"  +{len(pings) - 2} in the expandable board",) if len(pings) > 2 else ()),
            )),
        )
