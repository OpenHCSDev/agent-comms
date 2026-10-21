"""Service-owned commander review: changed standing rules wake through the real inbox."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_comms.message_bus import WatchPass
from agent_comms.registration import Registration
from agent_comms.service_sender import ServiceSender
from agent_comms.standing_rules import StandingRule
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.threads import Thread

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.goal_management import Goals
    from agent_comms.todos import Todo


@dataclass
class CommanderReview:
    """One commander's observed violations and its next review position."""

    violations: frozenset[StandingRule]
    turn: int = 0

    def wake(self, comms: Comms, sender: str, commander: Thread,
             rules: tuple[StandingRule, ...]) -> None:
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


@dataclass(frozen=True)
class CommanderObservation:
    thread: Thread
    merged: tuple[Todo, ...]
    rules: tuple[StandingRule, ...]


@dataclass
class CommanderHeartbeat(ServiceSender, WatchPass[tuple[CommanderObservation, ...]]):
    sender = "commander-heartbeat"
    reviews: dict[ThreadIncarnation, CommanderReview] = field(default_factory=dict)

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

    def observe(self, comms: Comms) -> tuple[CommanderObservation, ...]:
        return tuple(
            CommanderObservation(
                thread,
                comms.goals.todos.merged_orders(thread.incarnation, comms.registry),
                StandingRule.broken(comms, thread.name),
            )
            for thread in self.commanders(comms.registry, comms.goals)
        )

    def pass_body(self, comms: Comms, observed: tuple[CommanderObservation, ...]) -> None:
        """Inactive commanders retire their review after the pass enters the bus."""
        self.register(comms.registry)
        live = {item.thread.incarnation for item in observed}
        self.reviews = {
            owner: review for owner, review in self.reviews.items() if owner in live
        }
        for item in observed:
            comms.goals.todos.record_merges(item.merged)
            review = self.reviews.setdefault(item.thread.incarnation, CommanderReview(frozenset()))
            review.wake(comms, self.sender, item.thread, item.rules)
