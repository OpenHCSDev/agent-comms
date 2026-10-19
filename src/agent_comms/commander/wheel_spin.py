"""Core gives a stalled or unowned order to an idle unit; the stall watcher and the commander's
standing rules share it, so this module loads nothing that opens a bus on import."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from ..comms import Comms
from ..coordinator import Coordination
from ..goal_actions import OrderGoalAction
from ..standing_rules import StandingRule, UndatedStandingRule
from ..threads import Thread
from ..todos import Todo
from ..turn_digests import TurnDigestRead, TurnsSince
from .config import CommanderConfig


@dataclass(frozen=True)
class WheelSpin(StandingRule, declared_name=None):
    """An order is not getting closer to done, or a unit sits with no order; Core gives the
    order to a unit that will move it, with no deadline and no commander turn involved."""

    todo: Todo

    recent: ClassVar[int] = 10 * 60

    @classmethod
    def idle(cls, comms: Comms, commander: str) -> list[Thread]:
        """Units with a native turn in the last ten minutes and no order, parked threads aside."""
        held = {todo.holder for todo in cls.todos(comms, commander)}
        excluded = held | CommanderConfig.installed().parked | {commander, ""}
        bound = TurnsSince(time.time() - cls.recent)
        return sorted((thread for thread in comms.registry.all_threads().values()
                       if thread.name not in excluded
                       and TurnDigestRead(comms.root, thread, bound).native()),
                      key=lambda thread: thread.name)

    def assign(self, comms: Comms, commander: str, unit: Thread) -> None:
        """The order sent again to ``unit`` through its Goal, with the order's own text."""
        todo = comms.goals.todos.get(self.todo.id)
        order = todo.order.reissued(time.time())
        comms.goals.update_goal(unit.name, OrderGoalAction(
            problem=todo.id, expected_revision=todo.revision, order=order,
            request=todo.goal.request(), deadline=todo.deadline, commander=commander,
            traced=todo.order.owner))
        comms.messaging.send(commander, unit.name, f"{self.line()}\n{order.instructions(todo)}")



@dataclass(frozen=True)
class UnownedWheelSpin(WheelSpin, UndatedStandingRule):
    """A unit holds no next order while the board has unowned rows."""

    unit: str

    @classmethod
    def violations(cls, comms: Comms, commander: str) -> tuple[UnownedWheelSpin, ...]:
        return cls.paired(comms, commander, comms.goals.todos.for_commander(
            comms.registry.require(commander).incarnation))

    @classmethod
    def paired(cls, comms: Comms, commander: str, todos: Iterable[Todo]) -> tuple[UnownedWheelSpin, ...]:
        """Each unowned pending row with the first idle unit not struck on it."""
        rows = (todo for todo in todos if todo.pending and not todo.holder)
        idle, found = cls.idle(comms, commander), []
        for todo in rows:  # a unit struck on a problem never gets that problem back
            for unit in idle:
                if not todo.live_strikes(unit.name):
                    idle.remove(unit)
                    found.append(cls(todo, unit.name))
                    break
        return tuple(found)

    def line(self) -> str:
        return (f"WHEEL SPIN: {self.unit} holds no order while {self.todo.id} is unowned"
                f" -> Core gives it to {self.unit}")

    def enforce(self, comms: Comms, commander: str) -> None:
        self.assign(comms, commander, comms.registry.require(self.unit))

    @classmethod
    def after_stall(cls, comms: Comms) -> None:
        """One stall watcher pass: strike and release stalled orders, then give each released
        order to an idle unit for its own commander, with no commander turn involved."""
        released = comms.goals.todos.advance_stalled(comms.registry.storage_rule)
        for commander in {todo.creator_identity.name for todo in released}:
            mine = (todo for todo in released if todo.creator_identity.name == commander)
            for rule in cls.paired(comms, commander, mine):
                rule.enforce(comms, commander)

    @classmethod
    async def watch(cls, comms: Comms) -> None:
        """The running Core service makes one stall pass a minute."""
        while True:
            await Coordination.run_worker(lambda: cls.after_stall(comms))
            await asyncio.sleep(60)
