"""Service-owned commander review: changed standing rules wake through the real inbox."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from .message_bus import WatchPass
from .registration import Registration

if TYPE_CHECKING:
    from .comms import Comms
    from .goal_management import Goals
from .standing_rules import StandingRule
from .thread_execution import ExternalThreadExecution
from .thread_identity import ThreadIncarnation
from .threads import Thread


@dataclass
class CommanderReview:
    """One commander's observed violations and its next review position."""

    violations: frozenset[StandingRule]
    turn: int = 0

    def wake(self, comms: Comms, sender: str, commander: Thread) -> None:
        rules = StandingRule.broken(comms, commander.name)
        found = frozenset(rules)
        previous = self.violations
        if found == previous:
            return
        # Mark the observation before any effect; an uncertain publication is not replayed.
        self.violations = found
        StandingRule.enforce_all(comms, commander.name,
                                 (rule for rule in rules if rule not in previous))
        lines = ["COMMANDER HEARTBEAT: standing-rule violations changed."]
        owed = comms.goals.todos.held_for_commander(commander.incarnation)
        units = sorted({todo.held().owner for todo in owed})
        if units:
            unit = units[self.turn % len(units)]
            orders = ", ".join(todo.id for todo in owed if todo.held().owner == unit)
            lines.append(f"Review next: @{unit} ({orders}); read its git state and newest turn.")
            self.turn += 1
        lines.extend(StandingRule.heartbeat_changes(previous, rules))
        if not found:
            lines.append("Standing rules clear.")
        comms.messaging.send(sender, commander.name, "\n".join(lines))


@dataclass
class CommanderHeartbeat(WatchPass):
    sender: ClassVar[str] = "commander-heartbeat"
    reviews: dict[ThreadIncarnation, CommanderReview] = field(default_factory=dict)

    @classmethod
    def register(cls, registry: Registration) -> None:
        """Declare the shared service sender before any worker or restart publication."""
        if cls.sender not in registry:
            registry.declare(
                registry.new_thread(
                    cls.sender,
                    frozenset(),
                    str(registry.store.path.parent),
                    execution=ExternalThreadExecution,
                )
            )

    @classmethod
    def commanders(cls, registry: Registration, goals: Goals) -> tuple[Thread, ...]:
        """The live request holders whose current incarnation owns commander work."""
        return tuple(
            thread
            for thread in registry.all_threads().values()
            if thread.process_alive
            and any(
                goal.order.open_requests(goal, goals, thread.incarnation) for goal in thread.goal
            )
        )

    def pass_body(self, comms: Comms) -> None:
        """Inactive commanders retire their review after the pass enters the bus."""
        self.register(comms.registry)
        commanders = self.commanders(comms.registry, comms.goals)
        live = {thread.incarnation for thread in commanders}
        self.reviews = {
            owner: review for owner, review in self.reviews.items() if owner in live
        }
        for thread in commanders:
            comms.goals.todos.reconcile_merges(thread.incarnation, comms.registry)
            if thread.incarnation not in self.reviews:
                # Retain the baseline before effects; startup also acts on blocked holders.
                rules = StandingRule.broken(comms, thread.name)
                self.reviews[thread.incarnation] = CommanderReview(frozenset(rules))
                StandingRule.enforce_all(comms, thread.name, rules)
            else:
                self.reviews[thread.incarnation].wake(comms, self.sender, thread)
