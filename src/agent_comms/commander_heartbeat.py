"""Service-owned commander review: changed standing rules wake through the real inbox."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import ClassVar

from .child_process import ProcessState
from .comms import Comms
from .standing_rules import StandingRule
from .thread_execution import ExternalThreadExecution
from .thread_identity import ThreadIncarnation
from .threads import Thread
from .todos import Todo


@dataclass
class HeartbeatReview:
    """Heartbeat observations of one owner process and its commander rules."""

    process: ProcessState
    violations: frozenset[StandingRule]
    turn: int = 0

    def wake_restarted(self, comms: Comms, sender: str, unit: Thread,
                       orders: tuple[Todo, ...]) -> None:
        if self.process == unit.process_identity:
            return
        # Record before publication: a failed append must never replay a wake.
        self.process = unit.process_identity
        if orders:
            identities = ", ".join(sorted(todo.order.id for todo in orders))
            comms.messaging.send(sender, unit.name,
                f"Restarted holding {identities}; continue them from your branch")

    def wake(self, comms: Comms, sender: str, commander: Thread) -> None:
        rules = StandingRule.broken(comms, commander.name)
        found = frozenset(rules)
        if found == self.violations:
            return
        lines = ["COMMANDER HEARTBEAT: standing-rule violations changed."]
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
    sender: ClassVar[str] = "commander-heartbeat"
    reviews: dict[ThreadIncarnation, HeartbeatReview] = field(default_factory=dict)

    @classmethod
    def register(cls, comms: Comms) -> None:
        """The service speaks as itself before any connector request or worker pass."""
        if cls.sender not in comms.registry:
            comms.registry.declare(
                comms.registry.new_thread(
                    cls.sender, frozenset(), str(comms.root), execution=ExternalThreadExecution
                )
            )

    def watch_pass(self, opened_wire: Callable[[], AbstractContextManager[Comms]]) -> None:
        """Observe process changes in run_worker; inbox wakes derive from current held orders."""
        with opened_wire() as comms:
            self.register(comms)
            threads = comms.registry.all_threads()
            commanders = tuple(
                thread
                for thread in threads.values()
                if thread.process_alive
                and any(
                    goal.order.open_requests(goal, comms.goals, thread.incarnation)
                    for goal in thread.goal
                )
            )
            for commander in commanders:
                comms.goals.todos.reconcile_merges(commander.incarnation, comms.registry.storage_rule)
            commanding = {thread.incarnation for thread in commanders}
            held = {
                thread.incarnation: tuple(
                    todo for commander in commanders
                    for todo in comms.goals.todos.held_by(commander.incarnation, thread.name)
                    if todo.pending and todo.held().owner_identity == thread.incarnation
                ) for thread in threads.values()
            }
            # Baseline all declared owners, including stopped ones. This distinguishes
            # a later process start from merely receiving a first order in the same process.
            # Keep the old process through a stopped gap, even if its commander also stops.
            retained = {thread.incarnation for thread in threads.values()}
            self.reviews = {
                owner: review for owner, review in self.reviews.items() if owner in retained
            }
            for thread in threads.values():
                if thread.incarnation not in self.reviews:
                    # Initial observation, including connector startup, only establishes a baseline.
                    self.reviews[thread.incarnation] = HeartbeatReview(
                        thread.process_identity,
                        frozenset(StandingRule.broken(comms, thread.name))
                        if thread.incarnation in commanding else frozenset(),
                    )
                elif thread.process_alive:
                    review = self.reviews[thread.incarnation]
                    review.wake_restarted(comms, self.sender, thread, held[thread.incarnation])
                    if thread.incarnation in commanding:
                        review.wake(comms, self.sender, thread)
