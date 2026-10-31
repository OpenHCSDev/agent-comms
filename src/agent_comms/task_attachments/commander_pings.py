"""Retained commander observations are independent of optional bus publication.

The heartbeat retains each observation before attempting delivery. Published messages own
only what was actually sent; the disposable HUD renders the same retained packet. Original
subject actions dispose of requests, never a delivery setting or publication attempt.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from agent_comms.locked_store import DocumentChange, DurableFile, LockedStore, PatternFile
from agent_comms.store_files import DependentStateTier, SingleFileStore
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.harness_delivery_states import DeliveredOutcome, Outcome
from agent_comms.commander_service import CommanderService
from agent_comms.errors import RelationViolationError
from agent_comms.disclosure_text import AccentTone, ViewHeading, ViewTable, WarningTone
from agent_comms.nominal.field_codec import FieldCodec, HudExclude, OrderId, PublicationExclude, ThreadLookup
from agent_comms.message_reference import MessageReference
from agent_comms.heartbeat_subjects import CommanderHistory, HeartbeatSubject
from agent_comms.messages import (
    CommanderUpdatePosition, ConversationRole, Message, MessageRole, NoticeRole,
)
from agent_comms.projection_source import ProducedRow
from agent_comms.recorded_time import EventTime, UnrecordedEventTime
from agent_comms.standing_rules import ServicePassFailing, StandingRule
from agent_comms.task_attachments.owner_notices import OwnerNotice
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.ui_model.message_category import HeartbeatCategory, MessageCategory
from agent_comms.ui_model.unit_state import AwaitingCommander, UnitState
from agent_comms_hooks.board import BoardEntry

if TYPE_CHECKING:
    from collections.abc import Iterator

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
    def owner_flagged(self) -> bool:
        return False

    @property
    @abstractmethod
    def key(self) -> object: ...

    @property
    @abstractmethod
    def alarm_kind(self) -> type[DeclaredFamily]: ...

    def acted(self, history: CommanderHistory) -> bool:
        """Each subject owns whether its original action clears this line."""
        began = self.since.recorded_at("A line")
        return any(subject.disposed(history, began) for subject in self.subjects)

    def undisposed(self, history: CommanderHistory, cut: float) -> bool:
        """Waiting on the commander past the cut, without a recorded subject disposition.
        Only such a line is restated as waiting and locks the verbs."""
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
        return BoardEntry(content.value, self.text, since, since + AwaitingCommander.seconds, self.owner_flagged)

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
    def owner_flagged(self) -> bool:
        return self.rule.owner_flagged

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
    """A derived observation and its open set, shared by retention, publication and HUD."""

    packet: CommanderUpdate
    open_pings: tuple[Ping, ...]


class PublicationFacts(DeclaredFamily, affix="Facts"):
    """The display evidence carried by an actual committed packet."""


@dataclass(frozen=True)
class UnpublishedFacts(PublicationFacts):
    """Historical and failure packets never claim display data they did not publish."""


@dataclass(frozen=True)
class PublishedFacts(PublicationFacts):
    digest: TextDigest

    @classmethod
    def of(cls, facts: object) -> Self:
        return cls(TextDigest(FieldCodec.comparison_digest(facts)))


@dataclass(frozen=True)
class CommanderUpdate(OwnerNotice, declared_name=None):
    commander: ThreadIncarnation
    handled: tuple[Ping, ...]
    not_applied: tuple[str, ...] = field(default=(), kw_only=True)
    publication: PublicationFacts = field(default_factory=UnpublishedFacts, compare=False, kw_only=True)
    role: ClassVar[MessageRole]

    writes: ClassVar[tuple[type[ProducedRow], ...]] = (CommanderUpdatePosition,)

    def projections(self, message: Message) -> Iterator[TypedTable]:
        """The packet's position under the commander birth it was published for."""
        yield CommanderUpdatePosition(message.seq, self.commander.require_recorded().lookup)

    def commander_update(self, commander: ThreadLookup) -> tuple[CommanderUpdate, ...]:
        return (self,) if self.commander.require_recorded().lookup == commander else ()

    def category(self, spoken: type[MessageCategory]) -> type[MessageCategory]:
        """A board packet speaks in the owner's voice yet is not his words: its own category."""
        return HeartbeatCategory

    def should_publish(self, facts: PublishedFacts, previous: tuple[CommanderUpdate, ...]) -> bool:
        return True

    def new_pings(self) -> tuple[Ping, ...]:
        return ()

    def body(self, hud: tuple[str, ...]) -> str:
        """The existing HUD owns presentation; only a sent message claims this text."""
        return "\n".join((
            "BOARD (Core heartbeat; Tristan's own words never arrive in this format)",
            "Each rule prints its shared words once; '…' in a header is each row's own words.",
            *hud, "",
            "Full evidence: comms_hud board/open_pings, or Toad /commander original details",
        ))

    def board(self, comms: Comms) -> tuple[BoardEntry, ...]:
        """Any commander's recorded action disposes of its subject on the shared board."""
        history = CommanderHistory.read(comms, CommanderService.actors(comms.registry.snapshot()))
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

    def require_target(self, target: str, registry: RegistrySnapshot) -> None:
        if registry.require(target).incarnation != self.commander:
            raise RelationViolationError("Commander observations changed recipient incarnation")

    @classmethod
    def latest(cls, comms: Comms, commander: ThreadIncarnation) -> tuple[CommanderUpdate, ...]:
        """The last retained observation, even when nothing was sent to the bus."""
        return CommanderUpdateStore.at(comms.root, commander).read()

    @classmethod
    def published(cls, comms: Comms, commander: ThreadIncarnation) -> tuple[CommanderUpdate, ...]:
        """The last actual publication: what was announced, not just observed."""
        lookup = commander.require_recorded().lookup
        with comms.bus.bus.observing() as db:
            messages = CommanderUpdatePosition.latest(db, lookup)
        return tuple(packet for message in messages
                     for packet in message.task.commander_update(lookup))

    @classmethod
    def current_board(cls, comms: Comms, commander: str) -> tuple[BoardEntry, ...]:
        """The retained observation's board, derived at the moment of asking."""
        return tuple(entry for latest in cls.latest(comms, comms.registry.require(commander).incarnation)
                     for entry in latest.board(comms))

    @classmethod
    def derive(cls, commander: ThreadIncarnation, states: tuple[UnitState, ...],
               rules: tuple[StandingRule, ...],
               previous: tuple[CommanderUpdate, ...], *,
               published: tuple[CommanderUpdate, ...], now: float,
               history: CommanderHistory,
               not_applied: tuple[str, ...] = ()) -> CommanderDelivery:
        """Observation owns ages; actual publication owns which lines were announced.
        Publication never disposes of a ping; open requests still wake on every beat."""
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
        announced = {ping.key: ping for packet in published for ping in packet.handled}
        new = tuple(ping for ping in handled if announced.get(ping.key) != ping)
        notice = CommanderNotice(commander, handled, not_applied=not_applied)
        opened = notice.open_pings(history)
        open_members = frozenset(opened)
        packet = CommanderDecisions(
            commander, handled, new,
            tuple(ping for old in previous for ping in old.handled
                  if ping in open_members and ping.waits_on_commander(now - AwaitingCommander.seconds)),
            not_applied=not_applied,
        ) if new or opened or not_applied else notice
        return CommanderDelivery(packet, opened)

    @classmethod
    def report_failure(cls, commander: ThreadIncarnation, failure: ServicePassFailing,
                       previous: tuple[CommanderUpdate, ...], *,
                       published: tuple[CommanderUpdate, ...],
                       now: float) -> tuple[CommanderUpdate, ...]:
        """A failed pass clears nothing. Keep its original age, and retry an occurrence
        not yet published even when a muted/failed attempt already retained it."""
        ping = RulePing.capture(failure)
        if any(ping in packet.handled for packet in published):
            return ()
        handled = tuple(line for packet in previous for line in packet.handled)
        retained = (handled if ping in handled else
                    (*handled, replace(ping, since=ping.since.continued_from(now))))
        current = next(line for line in retained if line == ping)
        return (CommanderDecisions(
            commander, retained, (current,), (),
            not_applied=tuple(line for packet in previous for line in packet.not_applied),
        ),)

    def send(self, comms: Comms, hud: tuple[str, ...], *,
             publication: PublicationFacts = UnpublishedFacts()) -> tuple[Message, ...]:
        recipient = comms.registry.require(self.commander.name)
        return recipient.heartbeat_delivery.publish(
            replace(self, publication=publication), comms, hud)



@dataclass(frozen=True)
class CommanderNotice(CommanderUpdate):
    """Publish a changed quiet frame once, without waking for an identical copy."""

    role = NoticeRole()

    def should_publish(self, facts: PublishedFacts, previous: tuple[CommanderUpdate, ...]) -> bool:
        return not any(packet.publication == facts for packet in previous)

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


@dataclass(frozen=True, slots=True)
class CommanderUpdateStore(PatternFile, SingleFileStore, DurableFile,
                           LockedStore[tuple[CommanderUpdate, ...]], DependentStateTier):
    """The last observation for one commander birth, not a message or a disposable view.

    The packet declarations supply the complete stored shape through FieldCodec. Only the
    heartbeat writes it; failed publication and muted delivery do not erase observations.
    """

    filename: ClassVar[str] = "commander_updates/{lookup}.json"
    record_type = tuple[CommanderUpdate, ...]

    @classmethod
    def at(cls, root: Path, commander: ThreadIncarnation) -> Self:
        return cls(cls.path_in(root, lookup=str(commander.require_recorded().lookup)))

    def empty(self) -> tuple[CommanderUpdate, ...]:
        return ()

    @classmethod
    def record(cls, root: Path, packet: CommanderUpdate) -> None:
        store = cls.at(root, packet.commander)
        store.update(lambda previous: DocumentChange.to((packet,)))


class AlarmGroup(ViewHeading, affix="Alarms"):
    """Alarm membership shares heading behavior, not the unit-group registry."""

    @classmethod
    @abstractmethod
    def members(cls, packets: tuple[CommanderUpdate, ...],
                opened: tuple[Ping, ...], new: tuple[Ping, ...]) -> tuple[Ping, ...]: ...

    @classmethod
    def needs_disposal(cls) -> bool:
        """The heartbeat prints these above the unit tables; the rest follow them."""
        return True

    @classmethod
    def sections(cls, packets: tuple[CommanderUpdate, ...],
                 opened: tuple[Ping, ...]) -> tuple[AlarmSection, ...]:
        new = tuple(line for packet in packets for ping in packet.new_pings()
                    for line in ping.alarm_pings())
        return tuple(AlarmSection(group, members)
                     for group in cls.members_with(cls)
                     for members in (group.members(packets, opened, new),) if members)


class NewAlarms(AlarmGroup):
    tone = AccentTone

    @classmethod
    def members(cls, packets: tuple[CommanderUpdate, ...],
                opened: tuple[Ping, ...], new: tuple[Ping, ...]) -> tuple[Ping, ...]:
        return new


class StillOpenAlarms(AlarmGroup):
    tone = WarningTone

    @classmethod
    def members(cls, packets: tuple[CommanderUpdate, ...],
                opened: tuple[Ping, ...], new: tuple[Ping, ...]) -> tuple[Ping, ...]:
        new_members = frozenset(new)
        return tuple(line for ping in opened for line in ping.alarm_pings() if line not in new_members)


class WatchingAlarms(AlarmGroup):
    """A still-failing source remains visible, but it has no commander disposal to wake for."""

    @classmethod
    def needs_disposal(cls) -> bool:
        return False

    @classmethod
    def members(cls, packets: tuple[CommanderUpdate, ...],
                opened: tuple[Ping, ...], new: tuple[Ping, ...]) -> tuple[Ping, ...]:
        new_members = frozenset(new)
        return tuple(line for packet in packets for ping in packet.handled
                     if not ping.commander_disposes for line in ping.alarm_pings() if line not in new_members)


@dataclass(frozen=True)
class AlarmLine:
    kind: type[DeclaredFamily]
    line: str
    age: Annotated[str, PublicationExclude()]


@dataclass(frozen=True)
class AlarmSection:
    group: type[AlarmGroup]
    pings: tuple[Ping, ...]

    def heading(self) -> str:
        return self.group.count_heading(len(self.pings))

    def by_kind(self) -> Mapping[type[DeclaredFamily], tuple[Ping, ...]]:
        """Rows of one rule share what they ask, so the reader takes them as one decision."""
        grouped: dict[type[DeclaredFamily], list[Ping]] = {}
        for ping in self.pings:
            grouped.setdefault(ping.alarm_kind, []).append(ping)
        return {kind: tuple(pings) for kind, pings in grouped.items()}

    def digest(self, now: float) -> str:
        """The section in one line: how many of each rule, and the line left longest, so the
        reader sees what is owed and for how long before reading any row."""
        oldest = min(self.pings, key=lambda ping: ping.since.chronology())
        words = oldest.text.partition(FoldedRule.arrow)[0].split(" ")
        named = " ".join(words[:14]) + (" …" if len(words) > 14 else "")
        counts = ", ".join(f"{len(pings)} {kind.declared_name.replace('_', ' ')}"
                           for kind, pings in self.by_kind().items())
        return f"{self.heading()}: {counts}. Oldest, {oldest.since.age(now)}: {named}"

    def text_lines(self, now: float) -> tuple[str, ...]:
        """Every line of every rule, its shared words printed once in the rule's header."""
        return tuple(line for kind, pings in self.by_kind().items()
                     for line in FoldedRule.of(kind, pings).lines(now))

    def tables(self, now: float) -> tuple[ViewTable[AlarmLine], ...]:
        return (ViewTable(AlarmLine, HudExclude, tuple(
            AlarmLine(ping.alarm_kind, ping.text, ping.since.age(now)) for ping in self.pings)),)


class ActionWord(ABC):
    """One word of an action a rule's rows share: written once in the rule's header."""

    @abstractmethod
    def label(self) -> str:
        """How the header prints this word."""

    def filled(self, row: RuleRow) -> tuple[str, ...]:
        """The row's own value for this word; a word every row shares has none."""
        return ()


@dataclass(frozen=True)
class SharedWord(ActionWord):
    word: str

    def label(self) -> str:
        return self.word


@dataclass(frozen=True)
class RowWord(ActionWord):
    """A word each row fills; the header prints its place."""

    position: int

    def label(self) -> str:
        return "…"

    def filled(self, row: RuleRow) -> tuple[str, ...]:
        return (row.action[self.position],)


@dataclass(frozen=True)
class FlagValue(RowWord):
    """A word each row fills after a shared ``--flag``: the header names it by the flag, and a
    row whose own words already show it prints nothing more."""

    flag: str

    def label(self) -> str:
        return self.flag.upper()

    def filled(self, row: RuleRow) -> tuple[str, ...]:
        value = row.action[self.position]
        return () if value in row.shown() else (f"{self.flag}={value}",)


@dataclass(frozen=True)
class RuleRow:
    """One ping's own words: what differs from the rest of its rule's rows."""

    ping: Ping
    words: tuple[str, ...]
    action: tuple[str, ...]

    def shown(self) -> str:
        """Its own words, each short commit spelled in full when its action names that commit,
        so the line carries the full head once instead of twice."""
        return " ".join(next((value for value in self.action if len(word) >= 7 and value.startswith(word)),
                             word) for word in self.words)


@dataclass(frozen=True)
class FoldedRule:
    """A rule's rows share their opening, their closing question and the shape of their action:
    those print once in the header, and each row keeps only its own words and values."""

    kind: type[DeclaredFamily]
    prefix: tuple[str, ...]
    suffix: tuple[str, ...]
    template: tuple[ActionWord, ...]
    rows: tuple[RuleRow, ...]

    arrow: ClassVar[str] = " -> "

    @classmethod
    def of(cls, kind: type[DeclaredFamily], pings: tuple[Ping, ...]) -> FoldedRule:
        split = tuple(ping.text.partition(cls.arrow) for ping in pings)
        words = tuple(tuple(observed.split(" ")) for observed, _, _ in split)
        actions = tuple(tuple(action.split(" ")) if arrow else () for _, arrow, action in split)
        # A lone row shares nothing with anyone: it keeps every word it has.
        room = min(len(row) for row in words) - 1 if len(pings) > 1 else 0
        prefix = cls.common(words, room)
        suffix = cls.common(tuple(row[::-1] for row in words), room - len(prefix))[::-1]
        template = cls.shared_action(actions) if len(pings) > 1 else ()
        return cls(kind, prefix, suffix, template, tuple(
            RuleRow(ping, row[len(prefix):len(row) - len(suffix)], action)
            for ping, row, action in zip(pings, words, actions)))

    @staticmethod
    def common(rows: tuple[tuple[str, ...], ...], room: int) -> tuple[str, ...]:
        shared: list[str] = []
        for column in zip(*rows):
            if len(shared) >= room or len(set(column)) != 1:
                break
            shared.append(column[0])
        return tuple(shared)

    @staticmethod
    def shared_action(actions: tuple[tuple[str, ...], ...]) -> tuple[ActionWord, ...]:
        """Actions of one length align word for word; any other set stays on its rows."""
        if len({len(action) for action in actions}) != 1 or not actions[0]:
            return ()
        template: list[ActionWord] = []
        for position, column in enumerate(zip(*actions)):
            before = template[-1].label() if template else ""
            template.append(
                SharedWord(column[0]) if len(set(column)) == 1
                else FlagValue(position, before[2:]) if before.startswith("--")
                else RowWord(position))
        return tuple(template)

    def header(self) -> str:
        head = f"{self.kind.declared_name} · {len(self.rows)}"
        shape = " ".join((*self.prefix, "…", *self.suffix)) if self.prefix or self.suffix else ""
        action = (f"{self.arrow}{' '.join(word.label() for word in self.template)}"
                  if self.template else "")
        return f"{head}: {shape}{action}" if shape or action else head

    def row_line(self, row: RuleRow, now: float) -> str:
        values = tuple(value for word in self.template for value in word.filled(row))
        own = f"{self.arrow}{' '.join(row.action)}" if row.action and not self.template else ""
        tail = f" [{' '.join(values)}]" if values else ""
        return f"  {row.shown()}{tail}{own} · {row.ping.since.age(now)}"

    def lines(self, now: float) -> tuple[str, ...]:
        return (self.header(), *(self.row_line(row, now) for row in self.rows))
