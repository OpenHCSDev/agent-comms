"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Self

from agent_comms.build_identity import RecordedBuildIdentity
from agent_comms.comms import Comms
from agent_comms.goal_actions import ReissuedOrderGoalAction
from agent_comms.goals import Goal
from agent_comms.message_reference import MessageReference
from agent_comms.message_bus import WatchPass
from agent_comms.standing_rules import (
    DatedStandingRule, OrderRefusingStandingRule, StandingRule, UndatedStandingRule,
)
from agent_comms.storage_rules import OrderCheckoutWork
from agent_comms.thread_identity import CoreWatcherIncarnation, ThreadIncarnation
from agent_comms.threads import Thread
from agent_comms.todos import PullRequest, PullRequests, Todo, UnmergedListedPullRequest
from agent_comms.turn_digests import TurnDigestRead, TurnsSince
from agent_comms.work_dispatch import ReusedWorkDispatch, WorkDispatch
from agent_comms.commander.config import CommanderConfig
from agent_comms.channel_targets import ThreadTarget
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.message_page import PageBounds
from agent_comms.read_basis import NamedViewer
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.work_branches import WorkBranches
from agent_comms.ui_model.unit_state import UnitState


@dataclass(frozen=True)
class InstallDue(UndatedStandingRule):
    """Each configured main not contained in the installed build needs an install."""

    why: str

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        config = CommanderConfig.read_root(comms.root)
        installed = RecordedBuildIdentity.installed()
        due: list[Self] = []
        for repo in config.repos:
            repo.git(repo.checkout, "fetch", "-q", "origin", "main")
            main = repo.git(repo.checkout, "rev-parse", "origin/main").strip()
            if not installed.contains(main, repo.checkout):
                recorded = installed.for_repository(repo)
                due.append(cls(
                    f"{repo.github.to_wire()}: main {main}, installed {recorded.commit}"))
        return tuple(due)

    def line(self) -> str:
        return (f"INSTALL DUE ({self.why}) -> the installer rehearses and installs main with "
                "agent-comms install and posts the result to the commander")



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
class UnownedWheelSpin(UndatedStandingRule):
    """A unit holds no next order while the board has unowned rows; Core hands the row to it
    without a commander turn, restarting the row's review window at the handoff."""

    todo: Todo

    recent: ClassVar[int] = 10 * 60

    @classmethod
    def idle(cls, comms: Comms, commander: str) -> list[Thread]:
        """Units with a recent turn in their own transcript and no order, parked threads aside."""
        held = {owner.name for owner in UnitState.holders(comms, commander)}
        excluded = held | CommanderConfig.read_root(comms.root).parked | {commander, ""}
        bound = TurnsSince(time.time() - cls.recent)
        return sorted((thread for thread in comms.registry.all_threads().values()
                       if thread.name not in excluded
                       and TurnDigestRead(comms.root, thread, bound).read().turns),
                      key=lambda thread: thread.name)

    @staticmethod
    def order_for(todo: Todo, commander: str, assigner: ThreadIncarnation) -> ReissuedOrderGoalAction:
        now = time.time()
        return ReissuedOrderGoalAction(
            problem=todo.id, expected_revision=todo.revision,
            order=todo.order.reissued(now), request=todo.goal.request(),
            review_at=todo.reissued_review(now), commander=commander, assigner=assigner,
            traced=todo.order.owner)

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
        return cls.paired(comms, commander, rows, actor)

    @classmethod
    def paired(cls, comms: Comms, commander: str, todos: Iterable[Todo],
               assigner: ThreadIncarnation) -> tuple[UnownedWheelSpin, ...]:
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
                        cls.order_for(todo, commander, assigner), unit)))
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
                for rule in UnownedWheelSpin.paired(
                    comms, name, mine, CoreWatcherIncarnation.watcher()
                )
            )
        return tuple(observed)

    def pass_body(self, comms: Comms, observed: tuple[UnownedObservation, ...]) -> None:
        for item in observed:
            StandingRule.enforce_all(comms, item.commander.name, (item.rule,))


class WaitingUnownedWheelSpin(UnownedWheelSpin, declared_name=None):
    """A row needs a new unit; a proposed dispatch name is not an idle thread."""



@dataclass(frozen=True)
class CheckoutUnreadable(UndatedStandingRule):
    """A holder's checkout git cannot open, without hiding every other unit."""
    unit: str
    checkout: Path
    error: str = field(compare=False)

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Self, ...]:
        storage = comms.registry.storage_rule
        holders = sorted({todo.held().owner for todo in cls.pending_todos(comms, commander)})
        return tuple(cls(holder, path, error) for holder in holders
                     for path in storage.checkout_paths(comms.registry.require(holder))
                     for error in WorkBranches(path).read_checkout().errors())

    def line(self) -> str:
        return (f"CHECKOUT UNREADABLE: {self.unit} {self.checkout}: {self.error}"
                " -> order the unit to repair its checkout; its work stays retained")


@dataclass(frozen=True)
class HeartbeatSilent(DatedStandingRule):
    """Main's liveness check; P433 must move this read to non-waking pass observations."""
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
class Unjudged(OrderRefusingStandingRule, DatedStandingRule):
    """One ready current head without a verdict on its held problem."""
    problem: str
    holder: str
    request: PullRequest
    seconds: ClassVar[int] = 10 * 60

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Unjudged, ...]:
        held = cls.todos(comms, commander)
        pulls = PullRequests(held)
        return tuple(cls(todo.id, todo.holder, request) for todo in held
                     for request in pulls.listed(todo.repo, UnmergedListedPullRequest, todo)
                     if request.status.reviewable and todo.head_verdict(request.head) is None)

    def began(self) -> float:
        return self.request.reviewable_at

    def line(self) -> str:
        age = max(0, int((time.time() - self.began()) / 60))
        return (f"UNJUDGED {self.problem} {self.holder} PR #{self.request.number} "
                f"head {self.request.head} ({age} min) "
                f"-> judge ok {self.problem} or judge back {self.problem}")

    def refusal(self, comms: Comms, answers: Sequence[MessageReference]) -> tuple[str, ...]:
        return (self.line(),) if time.time() - self.began() >= self.seconds else ()
