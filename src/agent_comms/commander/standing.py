"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Self

from agent_comms.build_identity import RecordedBuildIdentity
from agent_comms.bus import BusReader
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.questions import UnansweredUnitAsks
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.comms import Comms
from agent_comms.goal_actions import ReissuedOrderGoalAction
from agent_comms.message_bus import WatchPass
from agent_comms.message_page import MessageReadRequest
from agent_comms.messages import Message
from agent_comms.owner_requests import OwnerRequestId
from agent_comms.phase_evidence import PlanReading
from agent_comms.plan_phases import PlanPhase
from agent_comms.read_basis import RepliesDisplayScope
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.standing_rules import (
    DatedStandingRule,
    IndependentStandingRule,
    OwedAnswer,
    ReasonedStandingRule,
    StandingRule,
    UndatedStandingRule,
)
from agent_comms.storage_rules import OrderCheckoutWork
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.threads import Thread
from agent_comms.todos import OwnerRequest, Todo
from agent_comms.turn_digests import OrderEditCountRead, TurnDigestRead, TurnsSince
from agent_comms.ui_model.unit_state import AwaitingCommander, UnitState
from agent_comms.work_dispatch import ReusedWorkDispatch, WorkDispatch


@dataclass(frozen=True)
class InstallDue(IndependentStandingRule, UndatedStandingRule):
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
class RequestStalled(IndependentStandingRule, UndatedStandingRule):
    request: OwnerRequest

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[RequestStalled, ...]:
        thread = comms.registry.require(commander)
        store = comms.goals.todos
        return tuple(cls(request) for request in store.open_requests(thread.incarnation)
                     if not store.serving_orders(request.id))

    def line(self) -> str:
        return (f"REQUEST STALLED: {self.request.id.value} {self.request.text}"
                " -> order its next step or mark it done")


@dataclass(frozen=True)
class AnswersTo(MessageReadRequest[tuple[Message, ...]]):
    """The newest reply to an original from the requested participants."""

    scope: RepliesDisplayScope
    since: float
    empty: ClassVar[tuple[Message, ...]] = ()

    def read_opened(self, source: BusReader) -> tuple[Message, ...]:
        return source.latest_answer(
            self.scope, since=self.since,
        )


class PlanRule(IndependentStandingRule, UndatedStandingRule, declared_name=None):
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
            f" {self.reading.describe(self.phase.evidence)} -> {self.phase.task.instruction}"
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
    """Message debt shares its source read with unit observations."""

    unit: str

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState]) -> tuple[Self, ...]:
        return tuple(cls(message, state.unit.name) for state in observed for message in state.owed)

    @classmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], commander: str,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        return tuple(cls(message, owner.name)
                     for owner, questions in UnansweredUnitAsks.for_orders(
                         source, snapshot, commander, held, time.time(),
                     ).items()
                     for message in questions)

    def due(self, now: float) -> bool:
        return self.message.timestamp <= now - AwaitingCommander.seconds

    def line(self) -> str:
        age = max(0, int((time.time() - self.message.timestamp) // 60))
        return (f"UNANSWERED {self.unit}: message {self.cited}, "
                f"{age} min old -> commander must answer")


@dataclass(frozen=True)
class UnownedWheelSpin(IndependentStandingRule, UndatedStandingRule):
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
class HeartbeatSilent(IndependentStandingRule, DatedStandingRule, declared_name=None):
    """Missing or stale heartbeat service for the commander's current open requests.

    The heartbeat beats on every pass, so its silence is the failure; this rule reads the
    bus in the commander's own tool process, where a dead heartbeat cannot hide it.
    """

    seconds: ClassVar[int] = 180
    since: float

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return tuple(rule for member in cls.members_with(cls)
                     for rule in member.violations(comms, commander))

    @staticmethod
    def beats(comms: Comms, commander: str) -> tuple[Message, ...]:
        snapshot = comms.registry.snapshot()
        if snapshot.canonical_name(CommanderHeartbeat.sender) not in snapshot.threads:
            return ()
        return comms.bus.bus.latest_addressed(
            snapshot.names_for(CommanderHeartbeat.sender), snapshot.names_for(commander),
        )

    def began(self) -> float:
        return self.since


@dataclass(frozen=True)
class MissedHeartbeat(HeartbeatSilent):
    beat: int

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        requests = CommanderHeartbeat.requested_by(comms.registry.require(commander), comms.goals)
        if not requests:
            return ()
        now = time.time()
        found = tuple(
            cls(since=max(request.created_at, beat.timestamp) + cls.seconds, beat=beat.seq)
            for request in requests for beat in cls.beats(comms, commander)
        )
        return tuple(rule for rule in found if rule.since < now)

    def line(self) -> str:
        return (f"HEARTBEAT SILENT: no beat since seq {self.beat}, silent past "
                f"{RecordedEventTime(self.since).clock()}"
                " -> read the connector journal for the failing pass and fix its owner")


@dataclass(frozen=True)
class HeartbeatNotStarted(HeartbeatSilent):
    request: OwnerRequestId

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        requests = CommanderHeartbeat.requested_by(comms.registry.require(commander), comms.goals)
        if not requests or cls.beats(comms, commander):
            return ()
        now = time.time()
        found = tuple(cls(since=request.created_at + cls.seconds, request=request.id)
                      for request in requests)
        return tuple(rule for rule in found if rule.since < now)

    def line(self) -> str:
        return (f"HEARTBEAT NOT STARTED: no beat for request {self.request.value}, overdue since "
                f"{RecordedEventTime(self.since).clock()}"
                " -> start the connector heartbeat and fix its failing pass")

@dataclass(frozen=True)
class Unjudged(ReasonedStandingRule, UndatedStandingRule):
    limit: ClassVar[int] = 30
    text = ("UNJUDGED {why} edits -> accept each reviewed PR head with judge head (one verdict"
            " judges every edit before it), or fork judges for the rest")

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        total = OrderEditCountRead.count(comms, commander)
        return str(total) if total > cls.limit else None
