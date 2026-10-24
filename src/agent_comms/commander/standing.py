"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.build_identity import RecordedBuildIdentity
from agent_comms.bus import BusReader
from agent_comms.channel_targets import CommsTag
from agent_comms.commander.questions import UnansweredUnitAsks
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.comms import Comms
from agent_comms.goal_actions import ReissuedOrderGoalAction
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.held_orders import HeldOrders
from agent_comms.message_bus import WatchPass
from agent_comms.message_page import MessageReadRequest
from agent_comms.messages import Message
from agent_comms.owner_requests import OwnerRequestId
from agent_comms.phase_evidence import PlanReading
from agent_comms.plan_phases import PlanPhase
from agent_comms.read_basis import RepliesDisplayScope
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.repository_name import RepositoryName
from agent_comms.service_passes import ServicePassStore
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
from agent_comms.todos import OrderClaim, OwnerRequest, Todo
from agent_comms.turn_digests import OrderEditCountRead, TurnDigestRead, TurnsSince
from agent_comms.ui_model.unit_state import AwaitingCommander, UnitState
from agent_comms.work_dispatch import ReusedWorkDispatch, WorkDispatch

if TYPE_CHECKING:
    from agent_comms.commander.order import CommanderVerb


@dataclass(frozen=True)
class InstallDue(IndependentStandingRule, UndatedStandingRule):
    """A configured main not contained in the installed build needs an install once its
    oldest uninstalled merge has stood for the settle window.

    Every install stops every running unit, so merges batch into one install instead of
    one install each. The window is read from the merges' own git times, not a schedule.
    """

    repo: RepositoryName
    main: str
    installed: str
    commits: int = field(compare=False)
    oldest_minutes: int = field(compare=False)
    settle: ClassVar[float] = 30 * 60

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        repos = comms.registry.snapshot().commander_project.require().repos
        installed = RecordedBuildIdentity.installed()
        now = time.time()
        due: list[Self] = []
        for repo in repos:
            repo.git(repo.checkout, "fetch", "-q", "origin", "main")
            main = repo.git(repo.checkout, "rev-parse", "origin/main").strip()
            if installed.contains(main, repo.checkout):
                continue
            recorded = installed.for_repository(repo)
            merged = repo.git(repo.checkout, "log", "--first-parent", "--format=%ct",
                              f"{recorded.commit}..{main}").split()
            waited = now - min(map(int, merged), default=now - cls.settle)
            if waited >= cls.settle:
                due.append(cls(repo.github, main, recorded.commit,
                               len(merged), int(waited // 60)))
        return tuple(due)

    def line(self) -> str:
        return (f"INSTALL DUE ({self.repo.to_wire()}: main {self.main}, installed "
                f"{self.installed}, {self.commits} commits, oldest {self.oldest_minutes} min) "
                "-> the installer rehearses and installs main with agent-comms install "
                "and posts the result to the commander")


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


class PlanRule(UndatedStandingRule, declared_name=None):
    """Rules read from the heartbeat's one look at the plans and the commander's orders."""

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        return PlanRule

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return tuple(rule for member in cls.members_with(cls) for rule in member.found(frame.plan))

    @classmethod
    @abstractmethod
    def found(cls, reading: PlanReading) -> tuple[Self, ...]: ...


@dataclass(frozen=True)
class PlanIdle(PlanRule):
    """A phase whose dependencies are done stalls unnoticed unless some order serves it."""

    phase: type[PlanPhase]
    reading: PlanReading = field(compare=False)

    @classmethod
    def found(cls, reading: PlanReading) -> tuple[Self, ...]:
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
    def found(cls, reading: PlanReading) -> tuple[Self, ...]:
        now = time.time()
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

    def questions_for(self, unit: ThreadIncarnation) -> tuple[Message, ...]:
        return (self.message,) if unit.name == self.unit else ()

    @classmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], commander: str,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        return tuple(cls(message, owner.name)
                     for owner, questions in UnansweredUnitAsks.for_orders(
                         source, snapshot, commander, held, time.time(),
                     ).items()
                     for message in questions)

    def clearing_action(self) -> str:
        return (f"Reply to {self.cited} or use --answers {self.message.message_id}. "
                'If the answer must wait, say "waiting on X, owner Y" now.')

    def due(self, now: float) -> bool:
        return self.message.timestamp <= now - AwaitingCommander.seconds

    def misaddressed(self, named: tuple[Message, ...], reached: frozenset[str]) -> str:
        """A unit's ask is answered only by a message that reaches that unit."""
        if self.unit in reached or not self.answered_by(named):
            return ""
        return (f"{self.message.message_id} was asked by {self.unit}; "
                f"answer it in a message to {self.unit}")

    def line(self) -> str:
        age = max(0, int((time.time() - self.message.timestamp) // 60))
        return (f"UNANSWERED {self.unit}: message {self.cited}, "
                f"{age} min old -> commander must answer")


@dataclass(frozen=True)
class DeliveryAwaitingJudgement(OwedAnswer, UndatedStandingRule):
    """A holder's delivery needs a verdict or a replacement order, never a chat reply."""

    unit: str
    problem: str

    @classmethod
    def read_open(cls, source: BusReader, snapshot: RegistrySnapshot,
                  held: Mapping[ThreadIncarnation, Sequence[Todo]], commander: str,
                  requests: Sequence[OwnerRequest]) -> tuple[Self, ...]:
        originals = OrderClaim.originals(
            source,
            {(owner, todo.id): todo.order.commander_claims()
             for owner, todos in held.items() for todo in todos},
        )
        return tuple(cls(message, owner.name, problem)
                     for (owner, problem), messages in originals.items() for message in messages)

    def clearing_action(self) -> str:
        return (f"order done {self.unit} {self.problem} (with --seen FILE for an application run)"
                " or send the unit its replacement order.")

    def answered_by(self, named: tuple[Message, ...]) -> bool:
        return False

    def blocks(self, reached: frozenset[str], action: CommanderVerb) -> bool:
        return self.unit in reached and not action.resolves_claim(self.unit, self.problem)

    def line(self) -> str:
        return (f"DELIVERY AWAITING JUDGEMENT {self.unit} {self.problem}: "
                f"message {self.cited} -> {self.clearing_action()}")


@dataclass(frozen=True)
class HolderGone(IndependentStandingRule, UndatedStandingRule):
    """An order still held by a unit that was deleted or registered again since the order:
    no registered thread can deliver it, so its order state names what clears it."""

    todo: Todo

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        return cls.in_scan(HeldOrders.read(comms, commander))

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return cls.in_scan(frame)

    @classmethod
    def in_scan(cls, scan: HeldOrders) -> tuple[Self, ...]:
        return tuple(cls(todo) for todo in scan.orphaned)

    def line(self) -> str:
        held = self.todo.held()
        return (f"HOLDER GONE: {self.todo.id} is held by {held.owner} born {held.owner_created}, "
                f"no longer registered -> {self.todo.order.orphan_clearing(self.todo)}")


@dataclass(frozen=True)
class UnownedWheelSpin(IndependentStandingRule, UndatedStandingRule):
    """A unit holds no next order while the board has unowned rows; Core hands the row to it
    without a commander turn, restarting the row's review window at the handoff."""

    todo: Todo

    recent: ClassVar[int] = 10 * 60

    @classmethod
    def idle(cls, comms: Comms, commander: str, scan: HeldOrders) -> list[Thread]:
        """Units with a recent turn in their own transcript and no order, parked threads aside."""
        held = {todo.holder for todo in scan.todos}
        excluded = held | {commander, ""}
        bound = TurnsSince(time.time() - cls.recent)
        return sorted((candidate for thread in comms.registry.all_threads().values()
                       if thread.name not in excluded
                       for candidate in thread.work_state.candidates(thread)
                       if TurnDigestRead(comms.root, candidate, bound).read().turns),
                      key=lambda thread: thread.name)

    def comparison_values(self) -> tuple[object, ...]:
        # Observation times and proposed idle units change; the row and its disposition do not.
        return (self.todo.id,)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, UnownedWheelSpin):
            return NotImplemented
        return type(self) is type(other) and self.comparison_values() == other.comparison_values()

    def __hash__(self) -> int:
        return hash((type(self), self.comparison_values()))

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[UnownedWheelSpin, ...]:
        return cls.scan_orders(comms, commander, HeldOrders.read(comms, commander))

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return cls.scan_orders(comms, commander, frame)

    @classmethod
    def scan_orders(cls, comms: Comms, commander: str,
                    scan: HeldOrders) -> tuple[UnownedWheelSpin, ...]:
        actor = comms.registry.require(commander).incarnation
        rows = tuple(todo for todo in comms.goals.todos.for_commander(actor)
                     if not todo.holder_state.current_assignments())
        return cls.paired(comms, commander, rows, scan)

    @classmethod
    def paired(cls, comms: Comms, commander: str,
               todos: Iterable[Todo], scan: HeldOrders) -> tuple[UnownedWheelSpin, ...]:
        """Both the heartbeat and automatic watcher preserve work before any dispatch."""
        rows = tuple(todo for todo in todos if todo.pending)
        retained = tuple(CheckoutHeldWheelSpin(todo, work)
                         for todo in rows
                         for work in comms.registry.storage_rule.work_for_problem(
                             todo, comms.registry, comms.goals.todos))
        protected = {rule.todo.id for rule in retained}
        idle, found = cls.idle(comms, commander, scan), []
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

    def comparison_values(self) -> tuple[object, ...]:
        return (*super().comparison_values(), self.work.owner.incarnation, self.work.checkout)

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
                for rule in UnownedWheelSpin.paired(
                    comms, name, mine, HeldOrders.read(comms, name),
                )
            )
        return tuple(observed)

    def pass_body(self, comms: Comms, observed: tuple[UnownedObservation, ...]) -> None:
        for item in observed:
            rejected = StandingRule.enforce_all(comms, item.commander.name, (item.rule,))
            for rule, error in rejected.items():
                logging.info("%s", rule.rejected_line(error))


class WaitingUnownedWheelSpin(UnownedWheelSpin, declared_name=None):
    """A row needs a new unit; a proposed dispatch name is not an idle thread."""


@dataclass(frozen=True)
class HeartbeatSilent(IndependentStandingRule, DatedStandingRule, declared_name=None):
    """A missing or stale successful pass, independent of whether a DM needed sending."""

    seconds: ClassVar[int] = 180
    since: float

    @classmethod
    def scan_owner(cls) -> type[StandingRule]:
        return HeartbeatSilent

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[HeartbeatSilent, ...]:
        owner = comms.registry.require(commander)
        completed = ServicePassStore.at(comms.root).read().commander_completed.get(owner.incarnation)
        now = time.time()
        found = []
        for request in CommanderHeartbeat.requested_by(owner, comms.goals):
            if completed is None or completed < request.created_at:
                rule = HeartbeatNotStarted(request.created_at + cls.seconds, request.id)
            else:
                rule = MissedHeartbeat(completed + cls.seconds)
            if rule.since < now:
                found.append(rule)
        return tuple(found)

    def began(self) -> float:
        return self.since


@dataclass(frozen=True)
class MissedHeartbeat(HeartbeatSilent):
    def line(self) -> str:
        return (f"HEARTBEAT SILENT: no completed pass since "
                f"{RecordedEventTime(self.since - self.seconds).clock()}, silent past "
                f"{RecordedEventTime(self.since).clock()}"
                " -> read the connector journal for the failing pass and fix its owner")


@dataclass(frozen=True)
class HeartbeatNotStarted(HeartbeatSilent):
    request: OwnerRequestId

    def line(self) -> str:
        return (f"HEARTBEAT NOT STARTED: no beat for request {self.request.value}, overdue since "
                f"{RecordedEventTime(self.since).clock()}"
                " -> start the connector heartbeat and fix its failing pass")


@dataclass(frozen=True)
class CommanderDeaf(IndependentStandingRule, DatedStandingRule):
    """The session bridge has no confirmed Monitor; its own registration dates the pause."""

    seconds: ClassVar[int] = 300
    since: float = field(compare=False)

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[CommanderDeaf, ...]:
        snapshot = comms.registry.snapshot()
        now = time.time()
        pauses = snapshot.status(commander).paused_since(snapshot.seen_at(commander))
        return tuple(cls(paused + cls.seconds)
                     for paused in pauses
                     if paused + cls.seconds < now)

    def began(self) -> float:
        return self.since

    def line(self) -> str:
        return (f"COMMANDER DEAF: its bridge has no confirmed Monitor since "
                f"{RecordedEventTime(self.since - self.seconds).clock()}, so every Core line"
                " to it waits unread -> re-arm its watch:"
                " agent-comms-claude-bridge watch (bare, no pipe)")

    def enforce(self, comms: Comms, commander: str) -> None:
        """The commander's own inbox is the dead path, so #comms hears it instead."""
        comms.messaging.send(
            CommanderHeartbeat.sender, CommsTag.channel(), f"{commander}: {self.line()}")

@dataclass(frozen=True)
class Unjudged(ReasonedStandingRule, UndatedStandingRule):
    limit: ClassVar[int] = 30
    text = ("UNJUDGED {why} edits -> accept each reviewed PR head with judge ok --pr N --head SHA"
            " (one verdict judges every edit before it), or fork judges for the rest")

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        total = OrderEditCountRead.count(comms, commander)
        return str(total) if total > cls.limit else None
