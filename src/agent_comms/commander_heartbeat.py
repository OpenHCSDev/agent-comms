"""Service-owned commander review: changed standing rules wake through the real inbox."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
import time

from .comms import Comms
from .standing_rules import StandingRule
from .thread_identity import ThreadIncarnation
from .threads import Thread


@dataclass
class CommanderReview:
    """One commander's last observed violations and its next review position."""

    violations: frozenset[StandingRule] = frozenset()
    turn: int = 0

    def wake(self, comms: Comms, sender: str, commander: Thread) -> None:
        rules = StandingRule.broken(comms, commander.name)
        reviewed = comms.goals.todos.review_predictions(
            commander.incarnation, comms.registry.storage_rule, time.time())
        found = frozenset(rules)
        if found == self.violations:
            return
        lines = ["COMMANDER HEARTBEAT: standing-rule violations changed.",
                 *(f"{todo.id} @{todo.holder}{todo.predicted_finish.line}" for todo in reviewed)]
        owed = comms.goals.todos.held_for_commander(commander.incarnation)
        units = sorted({todo.held().owner for todo in owed})
        if units:
            unit = units[self.turn % len(units)]
            orders = ", ".join(todo.id for todo in owed if todo.held().owner == unit)
            lines.append(f"Review next: @{unit} ({orders}); read its git state and newest turn.")
            self.turn += 1
        lines.extend(StandingRule.heartbeat_changes(self.violations, rules))
        if not found:
            lines.append("Standing rules clear.")
        # A publication can fail after append. Remember the attempted set before sending;
        # unchanged observations must not replay an uncertain side effect.
        self.violations = found
        comms.messaging.send(sender, commander.name, "\n".join(lines))


@dataclass
class CommanderHeartbeat:
    sender: str
    reviews: dict[ThreadIncarnation, CommanderReview] = field(default_factory=dict)

    def watch_pass(self, opened_wire: Callable[[], AbstractContextManager[Comms]]) -> None:
        """Borrow the bus only in run_worker; stopped/replaced commanders retire their review."""
        with opened_wire() as comms:
            commanders = tuple(
                thread
                for thread in comms.registry.all_threads().values()
                if thread.process_alive
                and any(
                    goal.order.open_requests(goal, comms.goals, thread.incarnation)
                    for goal in thread.goal
                )
            )
            live = {thread.incarnation for thread in commanders}
            self.reviews = {
                owner: review for owner, review in self.reviews.items() if owner in live
            }
            for thread in commanders:
                review = self.reviews.setdefault(thread.incarnation, CommanderReview())
                review.wake(comms, self.sender, thread)
