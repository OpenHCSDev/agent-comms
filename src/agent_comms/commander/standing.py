"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import ClassVar

from ..comms import Comms
from ..goal_actions import OrderGoalAction
from ..goals import Goal
from ..standing_rules import (
    DatedStandingRule,
    ReasonedStandingRule,
    StandingRule,
    UndatedStandingRule,
)
from ..thread_identity import StallWatcherIncarnation, ThreadIncarnation
from ..threads import Thread
from ..todos import FlatTrend, OrderProgress, PullRequests, Todo
from ..turn_digests import OrderEditCountRead, TurnDigestRead, TurnsSince
from ..work_dispatch import ReusedWorkDispatch, WorkDispatch
from .config import CommanderConfig


@dataclass(frozen=True)
class Detached(UndatedStandingRule):
    unit: str
    unread: int = field(compare=False)
    orders: tuple[str, ...]

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[Detached, ...]:
        owed: dict[str, list[str]] = {}
        for todo in cls.todos(comms, commander):
            owed.setdefault(todo.holder, []).append(todo.id)
        registry = comms.registry.snapshot()
        away = [unit for unit in sorted(owed)
                if unit not in registry.threads or not registry.status(unit).receiving]
        unread = comms.bus.pending_counts_all(tuple(unit for unit in away if unit in registry.threads))
        return tuple(cls(unit, unread.get(unit, 0), tuple(owed[unit])) for unit in away)

    def line(self) -> str:
        return (f"DETACHED {self.unit}: {self.unread} unread, orders {', '.join(self.orders)}"
                " -> wake it or reassign")


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
class WheelSpin(StandingRule, declared_name=None):
    """An order is not getting closer to done, or a unit sits with no order; Core gives the
    order to a unit that will move it, with no deadline and no commander turn involved."""

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

    @staticmethod
    def order_for(todo: Todo, commander: str, assigner: ThreadIncarnation) -> OrderGoalAction:
        return OrderGoalAction(
            problem=todo.id, expected_revision=todo.revision,
            order=todo.order.reissued(time.time()), request=todo.goal.request(),
            deadline=todo.deadline, commander=commander, assigner=assigner,
            traced=todo.order.owner)

    @abstractmethod
    def dispatches(self, comms: Comms, commander: str) -> Iterable[WorkDispatch]: ...

    def enforce(self, comms: Comms, commander: str) -> None:
        for dispatch in self.dispatches(comms, commander):
            unit = dispatch.start(comms)
            comms.messaging.send(commander, unit.name,
                f"{self.line()}\n{dispatch.action.order.instructions(self.todo)}")


@dataclass(frozen=True)
class FlatWheelSpin(WheelSpin, DatedStandingRule):
    """Two distance readings with no fall while the unit kept working."""

    since: float

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[FlatWheelSpin, ...]:
        held = tuple(todo for todo in cls.todos(comms, commander) if todo.pending)
        pulls = PullRequests(held)
        storage = comms.registry.storage_rule
        trends = ((todo, OrderProgress.read(todo, storage, pulls).distance_readings().trend)
                  for todo in held)
        flat = ((todo, trend.since) for todo, trend in trends if isinstance(trend, FlatTrend))
        return tuple(cls(todo, since) for todo, since in flat
                     if TurnDigestRead(comms.root, comms.registry.require(todo.held().owner),
                                       TurnsSince(since)).read().turns)

    def began(self) -> float:
        return self.since

    def line(self) -> str:
        return (f"WHEEL SPIN: {self.todo.holder} kept working on {self.todo.id} while its distance"
                " to done did not fall across two readings -> Core strikes it and gives the order"
                " to an idle unit")

    def dispatches(self, comms: Comms, commander: str) -> Iterable[WorkDispatch]:
        for unit in self.idle(comms, commander)[:1]:
            yield ReusedWorkDispatch(self.order_for(
                self.todo, commander, StallWatcherIncarnation.watcher()), self.todo.revision, unit)


@dataclass(frozen=True)
class UnownedWheelSpin(WheelSpin, UndatedStandingRule):
    """A unit holds no next order while the board has unowned rows."""

    dispatch: WorkDispatch

    @property
    def unit(self) -> str:
        return self.dispatch.name

    def _identity(self) -> str:
        # The dispatch carries an order reissued at observation time, enforcing it bumps the
        # row's revision, and the proposed unit moves as units go idle: the violation is the row.
        return self.todo.id

    def __eq__(self, other: object) -> bool:
        return isinstance(other, UnownedWheelSpin) and self._identity() == other._identity()

    def __hash__(self) -> int:
        return hash(self._identity())

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[UnownedWheelSpin, ...]:
        actor = comms.registry.require(commander).incarnation
        rows = tuple(todo for todo in comms.goals.todos.for_commander(actor)
                     if todo.pending and not todo.holder)
        paired = cls.paired(comms, commander, rows, actor)
        assigned = {rule.todo.id for rule in paired}
        return paired + tuple(WaitingUnownedWheelSpin(todo, WorkDispatch(
            cls.order_for(todo, commander, actor), todo.revision))
            for todo in rows if todo.id not in assigned)

    @classmethod
    def paired(cls, comms: Comms, commander: str, todos: Iterable[Todo],
               assigner: ThreadIncarnation) -> tuple[UnownedWheelSpin, ...]:
        """Each pending row with the first idle unit not struck on it."""
        idle, found = cls.idle(comms, commander), []
        for todo in (todo for todo in todos if todo.pending):
            for unit in idle:
                if not todo.live_strikes(unit.name):
                    idle.remove(unit)
                    found.append(cls(todo, ReusedWorkDispatch(
                        cls.order_for(todo, commander, assigner), todo.revision, unit)))
                    break
        return tuple(found)

    def line(self) -> str:
        return (f"WHEEL SPIN: {self.unit} holds no order while {self.todo.id} is unowned"
                f" -> Core gives it to {self.unit}")

    def dispatches(self, comms: Comms, commander: str) -> Iterable[WorkDispatch]:
        return (self.dispatch,)

    @classmethod
    def after_stall(cls, comms: Comms) -> None:
        """Assign eligible work; refused replacements preserve the observed row."""
        available = comms.goals.todos.reassignable(comms.registry.storage_rule)
        for commander in {todo.creator_identity.name for todo in available}:
            mine = (todo for todo in available if todo.creator_identity.name == commander)
            for rule in cls.paired(comms, commander, mine, StallWatcherIncarnation.watcher()):
                rule.enforce(comms, commander)

    @classmethod
    def watch_pass(cls, opened_wire: Callable[[], AbstractContextManager[Comms]]) -> None:
        """The worker borrows the root for this pass, never while the timer is idle."""
        with opened_wire() as comms:
            comms.goals.todos.advance_objective_failures(comms.goals)
            cls.after_stall(comms)


class WaitingUnownedWheelSpin(UnownedWheelSpin, declared_name=None):
    """A row needs a new unit; a proposed dispatch name is not an idle thread."""

    def line(self) -> str:
        return f"UNOWNED ROW: {self.todo.id} waits for a new unit"

    def heartbeat_group(self) -> object:
        return type(self)

    def heartbeat_line(self, current: Sequence[WaitingUnownedWheelSpin]) -> str:
        rows = ", ".join(sorted(rule.todo.id for rule in current))
        return f"{len(current)} unowned rows wait for a new unit: {rows}"


@dataclass(frozen=True)
class Unjudged(ReasonedStandingRule, UndatedStandingRule):
    limit: ClassVar[int] = 30
    text = "UNJUDGED {why} edits -> judge or fork a judge slice"

    @classmethod
    def reason(cls, comms: Comms, commander: str) -> str | None:
        total = OrderEditCountRead.count(comms, commander)
        return str(total) if total > cls.limit else None
