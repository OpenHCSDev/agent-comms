"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import ClassVar, Self

from agent_comms.comms import Comms
from agent_comms.goal_actions import ReissuedOrderGoalAction
from agent_comms.goals import Goal
from agent_comms.message_reference import MessageReference
from agent_comms.message_bus import WatchPass
from agent_comms.standing_rules import (
    OrderRefusingStandingRule, ReasonedStandingRule, StandingRule, UndatedStandingRule,
)
from agent_comms.storage_rules import OrderCheckoutWork
from agent_comms.thread_identity import CoreWatcherIncarnation, ThreadIncarnation
from agent_comms.threads import Thread
from agent_comms.todos import Todo
from agent_comms.turn_digests import TurnDigestRead, TurnsSince
from agent_comms.work_dispatch import ReusedWorkDispatch, WorkDispatch
from agent_comms.commander.config import CommanderConfig
from agent_comms.ui_model.unit_state import UnitState


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

    def heartbeat_group(self) -> object:
        return type(self)

    def heartbeat_line(self, current: Sequence[WaitingUnownedWheelSpin]) -> str:
        rows = ", ".join(sorted(rule.todo.id for rule in current))
        return f"{len(current)} unowned rows wait for a new unit: {rows}"


@dataclass(frozen=True)
class Unjudged(OrderRefusingStandingRule, ReasonedStandingRule, UndatedStandingRule):
    limit: ClassVar[int] = 50
    text = "UNJUDGED {why} pushed heads -> judge the commits before sending more orders"

    def refusal(self, comms: Comms, answers: Sequence[MessageReference]) -> tuple[str, ...]:
        return (f"{self.why} unreviewed pushed heads exceed the {self.limit} head ceiling; "
                "judge is the way out: review the commits before sending more orders.",)

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        total = len(comms.goals.todos.unreviewed_heads(
            comms.registry.require(commander).incarnation, comms.registry))
        return str(total) if total > cls.limit else None
