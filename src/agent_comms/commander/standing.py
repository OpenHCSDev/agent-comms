"""Standing rules read from the commander's own files, members of Core's StandingRule family."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import ClassVar

from ..comms import Comms
from ..goal_actions import OrderGoalAction
from ..goals import Goal
from ..standing_rules import ReasonedStandingRule, UndatedStandingRule
from ..thread_identity import CoreWatcherIncarnation, ThreadIncarnation
from ..threads import Thread
from ..todos import Todo
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
        away = [unit for unit in sorted(owed) if not registry.receiving(unit)]
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
class UnownedWheelSpin(UndatedStandingRule):
    """A unit holds no next order while the board has unowned rows; Core hands the row to it
    without a commander turn, restarting the row's review window at the handoff."""

    todo: Todo
    dispatch: WorkDispatch

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
        now = time.time()
        return OrderGoalAction(
            problem=todo.id, expected_revision=todo.revision,
            order=todo.order.reissued(now), request=todo.goal.request(),
            review_at=todo.reissued_review(now), commander=commander, assigner=assigner,
            traced=todo.order.owner)

    def enforce(self, comms: Comms, commander: str) -> None:
        unit = self.dispatch.start(comms)
        comms.messaging.send(commander, unit.name,
            f"{self.line()}\n{self.dispatch.action.order.instructions(self.todo)}")

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

    @classmethod
    def after_release(cls, comms: Comms) -> None:
        """Hand work Core released to idle units; refused replacements preserve the row."""
        available = comms.goals.todos.reassignable()
        for commander in {todo.creator_identity.name for todo in available}:
            mine = (todo for todo in available if todo.creator_identity.name == commander)
            for rule in cls.paired(comms, commander, mine, CoreWatcherIncarnation.watcher()):
                rule.enforce(comms, commander)

    @classmethod
    def watch_pass(cls, opened_wire: Callable[[], AbstractContextManager[Comms]]) -> None:
        """The worker borrows the root for this pass, never while the timer is idle."""
        with opened_wire() as comms:
            cls.after_release(comms)


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
