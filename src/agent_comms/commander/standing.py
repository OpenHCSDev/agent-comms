"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from abc import abstractmethod
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Self

from agent_comms.build_identity import RecordedBuildIdentity
from agent_comms.comms import Comms
from agent_comms.goal_actions import ReissuedOrderGoalAction
from agent_comms.goals import Goal
from agent_comms.goal_presentation import BlockedExecution
from agent_comms.message_page import MessageReadRequest
from agent_comms.message_reference import MessageReference
from agent_comms.messages import Message
from agent_comms.read_basis import RepliesDisplayScope
from agent_comms.bus_page_index import BusPageIndex
from agent_comms.wire_log import WireSnapshot
from agent_comms.message_bus import WatchPass
from agent_comms.channel_targets import ThreadTarget
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.message_page import PageBounds
from agent_comms.read_basis import NamedViewer
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import (
    DatedStandingRule, OwedAnswer, ReasonedStandingRule, StandingRule, UndatedStandingRule,
)
from agent_comms.storage_rules import OrderCheckoutWork
from agent_comms.threads import Thread
from agent_comms.todos import Todo
from agent_comms.turn_digests import OrderEditCountRead, TurnDigestRead, TurnsSince
from agent_comms.work_branches import WorkBranches
from agent_comms.work_dispatch import ReusedWorkDispatch, WorkDispatch
from agent_comms.commander.config import CommanderConfig
from agent_comms.phase_evidence import PlanReading
from agent_comms.plan_phases import PlanPhase


@dataclass(frozen=True)
class InstallDue(UndatedStandingRule):
    """A configured main not contained in the installed build needs an install once its
    oldest uninstalled merge has stood for the settle window.

    Every install stops every running unit, so merges batch into one install instead of
    one install each. The window is read from the merges' own git times, not a schedule.
    """

    why: str
    settle: ClassVar[float] = 30 * 60

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        config = CommanderConfig.read_root(comms.root)
        installed = RecordedBuildIdentity.installed()
        now = time.time()
        due: list[Self] = []
        for repo in config.repos:
            repo.git(repo.checkout, "fetch", "-q", "origin", "main")
            main = repo.git(repo.checkout, "rev-parse", "origin/main").strip()
            if installed.contains(main, repo.checkout):
                continue
            recorded = installed.for_repository(repo)
            merged = repo.git(repo.checkout, "log", "--first-parent", "--format=%ct",
                              f"{recorded.commit}..{main}").split()
            waited = now - min(map(int, merged), default=now - cls.settle)
            if waited >= cls.settle:
                due.append(cls(
                    f"{repo.github.to_wire()}: main {main}, installed {recorded.commit}, "
                    f"{len(merged)} commits, oldest {int(waited // 60)} min"))
        return tuple(due)

    def line(self) -> str:
        return (f"INSTALL DUE ({self.why}) -> the installer rehearses and installs main with "
                "agent-comms install and posts the result to the commander")


@dataclass(frozen=True)
class HolderCannotRun(UndatedStandingRule, declared_name=None):
    unit: str
    orders: tuple[str, ...]

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        return HolderCannotRun

    @staticmethod
    @abstractmethod
    def stalled(comms: Comms, thread: Thread) -> bool: ...

    @classmethod
    @abstractmethod
    def found(cls, comms: Comms, thread: Thread, orders: tuple[str, ...]) -> Self: ...

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        registry = comms.registry.snapshot()
        found: list[Self] = []
        members = cls.members_with(cls)
        for owner, todos in cls.holders(comms, commander).items():
            thread = registry.require(owner.name)
            if thread.incarnation != owner:
                raise ValueError("Held order owner incarnation changed")
            orders = tuple(todo.id for todo in todos)
            found.extend(member.found(comms, thread, orders)
                         for member in members if member.stalled(comms, thread))
        return tuple(found)


@dataclass(frozen=True)
class Detached(HolderCannotRun):
    unread: int = field(compare=False)

    @staticmethod
    def stalled(comms: Comms, thread: Thread) -> bool:
        return not comms.registry.status(thread.name).receiving

    @classmethod
    def found(cls, comms: Comms, thread: Thread, orders: tuple[str, ...]) -> Self:
        return cls(thread.name, orders, comms.bus.pending_count(thread.name))

    def line(self) -> str:
        return (f"DETACHED {self.unit}: {self.unread} unread, orders {', '.join(self.orders)}"
                " -> wake it or reassign")


@dataclass(frozen=True)
class GoalCannotRun(UndatedStandingRule):
    """An ordered unit whose goal execution supplies the block and its action."""

    unit: str
    orders: tuple[str, ...]
    goal: Goal
    execution: BlockedExecution

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return tuple(
            cls(owner.name, tuple(todo.id for todo in todos), view.goal.require(), block)
            for owner, todos in cls.holders(comms, commander).items()
            for view in (comms.goals.goal_snapshot(owner.name),)
            for block in view.execution.unit_blocks()
        )

    def enforce(self, comms: Comms, commander: str) -> None:
        self.execution.enforce_unit_block(comms, commander, self.unit, self.orders, self.goal)

    def line(self) -> str:
        return self.execution.unit_block_line(self.unit, self.orders)


@dataclass(frozen=True)
class RequestStalled(UndatedStandingRule):
    goal: Goal

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[RequestStalled, ...]:
        thread = comms.registry.require(commander)
        return tuple(cls(request) for goal in thread.goal.active()
                     for request, served in goal.order.open_requests(
                         goal, comms.goals, thread.incarnation)
                     if not served)

    def line(self) -> str:
        return (f"REQUEST STALLED: {self.goal.id} {self.goal.text}"
                " -> order its next step or mark it done")


@dataclass(frozen=True)
class AnswersTo(MessageReadRequest[tuple[Message, ...]]):
    """The first participant's replies that answer a second participant's message, newest
    first; the page is read newest first, so an answer is seen before what it answers."""

    scope: RepliesDisplayScope
    since: float
    empty: ClassVar[tuple[Message, ...]] = ()

    def indexed(self, index: BusPageIndex, source: WireSnapshot) -> tuple[Message, ...]:
        replies: list[Message] = []
        asked: set[MessageReference] = set()
        with closing(index.recent_offsets(
            self.since, predicate=self.scope.index_predicate, before_offset=source.boundary,
        )) as offsets:
            for row in self.indexed_records(source, offsets):
                message = row.message
                if message.sender in self.scope.first_names:
                    replies.append(message)
                else:
                    asked.add(message.reference)
        return tuple(reply for reply in replies if asked.intersection(reply.answers))


@dataclass(frozen=True, slots=True)
class UnansweredUnitAsks(MessageReadRequest[tuple[Message, ...]]):
    scope: RepliesDisplayScope
    since: float
    before: float
    empty: ClassVar[tuple[Message, ...]] = ()

    def indexed(self, index: BusPageIndex, source: WireSnapshot) -> tuple[Message, ...]:
        answered: set[MessageReference] = set()
        found: list[Message] = []
        with closing(index.recent_offsets(
            self.since, predicate=self.scope.index_predicate, before_offset=source.boundary,
        )) as offsets:
            for row in self.indexed_records(source, offsets):
                message = row.message
                if message.sender in self.scope.first_names:
                    answered.update(message.answers)
                elif message.timestamp <= self.before and message.reference not in answered:
                    found.append(message)
        return tuple(found)


class PlanRule(UndatedStandingRule, declared_name=None):
    """A rule read from one look at the plans and the commander's orders."""

    @staticmethod
    def plan_reading(comms: Comms, commander: str) -> PlanReading:
        return CommanderConfig.read_root(comms.root).plan_reading(
            comms.goals.todos.history_for_commander(comms.registry.require(commander).incarnation)
        )


@dataclass(frozen=True)
class PlanIdle(PlanRule):
    """A phase whose dependencies are done stalls unnoticed unless some order serves it."""

    phase: type[PlanPhase]
    reading: PlanReading = field(compare=False)

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        reading = cls.plan_reading(comms, commander)
        return tuple(
            cls(phase, reading)
            for phase in PlanPhase.members_with(PlanPhase)
            if phase.ready(reading) and not phase.ordered(reading)
        )

    def enforce(self, comms: Comms, commander: str) -> None:
        self.phase.task.start(comms, comms.registry.require(commander), self.phase, self.reading)

    def line(self) -> str:
        return (
            f"PLAN IDLE: {self.phase.plan.declared_name} phase {self.phase.declared_name} ready"
            f" ({self.phase.plan.document}), no open order; done when"
            f" {self.phase.evidence.describe()} -> {self.phase.task.instruction}"
        )


@dataclass(frozen=True)
class PlanStalled(PlanRule):
    """A ready phase that a unit has held past the window while main still lacks it. Holding an
    order is not moving the plan: something structural keeps it still (steps that only land
    together, a blocked holder, a missing owner), and re-sending the order changes nothing."""

    phase: type[PlanPhase]
    since: float
    window: ClassVar[int] = 2 * 60 * 60

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        reading, now = cls.plan_reading(comms, commander), time.time()
        return tuple(
            cls(phase, since)
            for phase in PlanPhase.members_with(PlanPhase)
            if phase.ready(reading) and phase.ordered(reading)
            for since in (phase.ordered_since(reading),)
            if now - since >= cls.window
        )

    def line(self) -> str:
        hours = (time.time() - self.since) / 3600
        return (
            f"PLAN STALLED: {self.phase.plan.declared_name} phase {self.phase.declared_name}"
            f" held {hours:.1f}h and not on main ({self.phase.plan.document}) -> find what holds"
            " it still and change that this wake: integrate steps that only land together,"
            " take it over, or reassign it"
        )


@dataclass(frozen=True)
class UnitAskUnanswered(OwedAnswer, UndatedStandingRule):
    unit: str
    seconds: ClassVar[int] = 5 * 60

    @staticmethod
    def asks(comms: Comms, commander: str, before: float) -> tuple[Message, ...]:
        """Holders' messages to the commander, sent by ``before``, that no commander reply answers."""
        held = StandingRule.holders(comms, commander)
        if not held:
            return ()
        snapshot = comms.registry.snapshot()
        peers = frozenset(name for owner in held for name in snapshot.names_for(owner.name))
        since = min(todo.order.checked().at.recorded_at("A held order")
                    for todos in held.values() for todo in todos)
        return comms.bus.log.read_page(UnansweredUnitAsks(
            RepliesDisplayScope(snapshot.names_for(commander), peers), since, before,
        ))

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        snapshot = comms.registry.snapshot()
        return tuple(cls(message, snapshot.canonical_name(message.sender))
                     for message in cls.asks(comms, commander, time.time() - cls.seconds))

    @classmethod
    def open_messages(cls, comms: Comms, commander: str) -> tuple[Message, ...]:
        return cls.asks(comms, commander, time.time())

    def line(self) -> str:
        age = max(0, int((time.time() - self.message.timestamp) // 60))
        return (f"UNANSWERED {self.unit}: message {self.cited}, "
                f"{age} min old -> commander must answer")


@dataclass(frozen=True)
class UnownedWheelSpin(UndatedStandingRule):
    """A unit holds no next order while the board has unowned rows; Core hands the row to it
    without a commander turn, restarting the row's review window at the handoff."""

    todo: Todo

    recent: ClassVar[int] = 10 * 60

    @classmethod
    def idle(cls, comms: Comms, commander: str) -> list[Thread]:
        """Units with a recent turn in their own transcript and no order, parked threads aside."""
        held = {todo.holder for todo in cls.todos(comms, commander)}
        excluded = held | CommanderConfig.read_root(comms.root).parked | {commander, ""}
        bound = TurnsSince(time.time() - cls.recent)
        return sorted((thread for thread in comms.registry.all_threads().values()
                       if thread.name not in excluded
                       and TurnDigestRead(comms.root, thread, bound).read().turns),
                      key=lambda thread: thread.name)

    def _identity(self) -> tuple[object, ...]:
        # Observation times and proposed idle units change; the row and its disposition do not.
        return type(self), self.todo.id

    def __eq__(self, other: object) -> bool:
        return isinstance(other, UnownedWheelSpin) and self._identity() == other._identity()

    def __hash__(self) -> int:
        return hash(self._identity())

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[UnownedWheelSpin, ...]:
        actor = comms.registry.require(commander).incarnation
        rows = tuple(todo for todo in comms.goals.todos.for_commander(actor)
                     if not todo.holder_state.current_assignments())
        return cls.paired(comms, commander, rows)

    @classmethod
    def paired(cls, comms: Comms, commander: str,
               todos: Iterable[Todo]) -> tuple[UnownedWheelSpin, ...]:
        """Both the heartbeat and automatic watcher preserve work before any dispatch."""
        rows = tuple(todo for todo in todos if todo.pending)
        retained = tuple(CheckoutHeldWheelSpin(todo, work)
                         for todo in rows
                         for work in comms.registry.storage_rule.work_for_problem(
                             todo, comms.registry, comms.goals.todos))
        protected = {rule.todo.id for rule in retained}
        idle, found = cls.idle(comms, commander), []
        for todo in (todo for todo in rows if todo.id not in protected):
            for unit in idle:
                if not todo.live_strikes(unit.name):
                    idle.remove(unit)
                    found.append(ReassignedWheelSpin(todo, ReusedWorkDispatch.for_unit(
                        ReissuedOrderGoalAction.for_todo(todo, commander), unit)))
                    break
        covered = {rule.todo.id for rule in (*retained, *found)}
        return (*retained, *found,
                *(WaitingUnownedWheelSpin(todo) for todo in rows if todo.id not in covered))

    def line(self) -> str:
        return f"UNOWNED ROW: {self.todo.id} waits for a new unit"


@dataclass(frozen=True, eq=False)
class ReassignedWheelSpin(UnownedWheelSpin, declared_name=None):
    dispatch: WorkDispatch

    @property
    def unit(self) -> str:
        return self.dispatch.name

    def enforce(self, comms: Comms, commander: str) -> None:
        unit = self.dispatch.start(comms)
        comms.messaging.send(commander, unit.name,
            f"{self.line()}\n{self.dispatch.action.order.instructions(self.todo)}")

    def line(self) -> str:
        return (f"WHEEL SPIN: {self.unit} holds no order while {self.todo.id} is unowned"
                f" -> Core gives it to {self.unit}")


@dataclass(frozen=True, eq=False)
class CheckoutHeldWheelSpin(UnownedWheelSpin, declared_name=None):
    work: OrderCheckoutWork

    def _identity(self) -> tuple[object, ...]:
        return (*super()._identity(), self.work.owner.incarnation, self.work.checkout)

    def line(self) -> str:
        return (f"WORK HELD: {self.todo.id} {self.work.owner.name} retains work in "
                f"{self.work.checkout} -> restore its holder; do not hand the row away")


@dataclass(frozen=True)
class UnownedObservation:
    commander: Thread
    rule: UnownedWheelSpin


class UnownedOrderWatch(WatchPass[tuple[UnownedObservation, ...]]):
    """Observe released work and its checkouts before entering the bus to assign it."""

    def observe(self, comms: Comms) -> tuple[UnownedObservation, ...]:
        available = comms.goals.todos.reassignable()
        observed: list[UnownedObservation] = []
        for name in {todo.creator_identity.name for todo in available}:
            commander = comms.registry.require(name)
            mine = (todo for todo in available if todo.creator_identity.name == name)
            observed.extend(
                UnownedObservation(commander, rule)
                for rule in UnownedWheelSpin.paired(comms, name, mine)
            )
        return tuple(observed)

    def pass_body(self, comms: Comms, observed: tuple[UnownedObservation, ...]) -> None:
        for item in observed:
            StandingRule.enforce_all(comms, item.commander.name, (item.rule,))


class WaitingUnownedWheelSpin(UnownedWheelSpin, declared_name=None):
    """A row needs a new unit; a proposed dispatch name is not an idle thread."""

    def heartbeat_group(self) -> object:
        return type(self)

    def heartbeat_line(self, current: Sequence[WaitingUnownedWheelSpin]) -> str:
        rows = ", ".join(sorted(rule.todo.id for rule in current))
        return f"{len(current)} unowned rows wait for a new unit: {rows}"


@dataclass(frozen=True)
class CheckoutUnreadable(UndatedStandingRule):
    """A holder's checkout git cannot open: that unit's state, while every other unit reads on."""

    unit: str
    checkout: Path
    error: str = field(compare=False)

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        storage = comms.registry.storage_rule
        holders = sorted({todo.held().owner for todo in cls.pending_todos(comms, commander)})
        return tuple(
            cls(holder, path, error)
            for holder in holders
            for path in storage.checkout_paths(comms.registry.require(holder))
            for error in WorkBranches(path).read_checkout().errors()
        )

    def line(self) -> str:
        return (f"CHECKOUT UNREADABLE: {self.unit} {self.checkout}: {self.error}"
                " -> order the unit to repair its checkout; its work stays retained")

@dataclass(frozen=True)
class HeartbeatSilent(DatedStandingRule):
    """A commander the heartbeat serves whose newest beat is older than the cadence allows.

    The heartbeat beats on every pass, so its silence is the failure; this rule reads the
    bus in the commander's own tool process, where a dead heartbeat cannot hide it.
    """

    seconds: ClassVar[int] = 180
    beat: int
    since: float

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        served = {thread.incarnation
                  for thread in CommanderHeartbeat.commanders(comms.registry, comms.goals)}
        if CommanderHeartbeat.sender not in comms.registry or (
                comms.registry.require(commander).incarnation not in served):
            return ()
        page = comms.views.history_page(ThreadTarget(CommanderHeartbeat.sender),
                                        PageBounds(limit=1), viewer=NamedViewer(commander))
        return tuple(cls(beat.seq, beat.timestamp + cls.seconds) for beat in page.messages[-1:]
                     if beat.sender == CommanderHeartbeat.sender
                     and beat.timestamp + cls.seconds < time.time())

    def began(self) -> float:
        return self.since

    def line(self) -> str:
        return (f"HEARTBEAT SILENT: no beat since seq {self.beat}, silent past "
                f"{RecordedEventTime(self.since).clock()}"
                " -> read the connector journal for the failing pass and fix its owner")

@dataclass(frozen=True)
class Unjudged(ReasonedStandingRule, UndatedStandingRule):
    limit: ClassVar[int] = 30
    text = "UNJUDGED {why} edits -> judge or fork a judge slice"

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        total = OrderEditCountRead.count(comms, commander)
        return str(total) if total > cls.limit else None
