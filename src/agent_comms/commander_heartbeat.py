"""Service-owned commander review: changed standing rules wake through the real inbox."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.message_bus import WatchPass
from agent_comms.registration import Registration
from agent_comms.service_sender import ServiceSender
from agent_comms.standing_rules import StandingRule
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.threads import Thread
from agent_comms.todos import ProgressRead
from agent_comms.ui_model.unit_state import UnitState

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.goal_management import Goals
    from agent_comms.todos import OwnerRequest, Todo


@dataclass
class CommanderReview:
    """One commander's handled observations and its last changed beat.

    Seq 0 precedes every published message: no changed beat has been sent yet.
    """

    rules: frozenset[StandingRule]
    states: frozenset[UnitState] = frozenset()
    changed: int = 0

    def wake(self, comms: Comms, sender: str, commander: Thread,
             rules: tuple[StandingRule, ...], states: tuple[UnitState, ...]) -> None:
        """Every pass beats, so silence is a failure; an unchanged beat is one short line."""
        found_rules, found_states = frozenset(rules), frozenset(states)
        actionable = (*rules, *(state for state in states if state.attention()))
        old_rules = self.rules
        previous = self.rules | self.states
        if found_rules == self.rules and found_states == self.states and self.changed:
            comms.messaging.send(sender, commander.name,
                                 f"COMMANDER HEARTBEAT: unchanged since seq {self.changed},"
                                 f" {len(actionable)} violations.")
            return
        # Mark the observation before any effect; an uncertain publication is not replayed.
        self.rules, self.states = found_rules, found_states
        rejected = StandingRule.enforce_all(
            comms, commander.name, (rule for rule in actionable if rule not in previous)
        )
        # A checked rejection has no effects and must not suppress a fresh observation.
        self.rules -= rejected
        self.states -= rejected
        lines = ["COMMANDER HEARTBEAT: unit states and standing rules changed."]
        lines.extend(state.line() for state in states)
        lines.extend(StandingRule.heartbeat_changes(old_rules, self.rules))
        lines.extend(rule.rejected_line() for rule in actionable if rule in rejected)
        if not actionable:
            lines.append("Standing rules clear.")
        self.changed = comms.messaging.send(sender, commander.name, "\n".join(lines)).message.seq


@dataclass(frozen=True)
class CommanderObservation:
    thread: Thread
    merged: tuple[Todo, ...]
    rules: tuple[StandingRule, ...]
    states: tuple[UnitState, ...]

    @classmethod
    def read(cls, comms: Comms, thread: Thread,
             reading: type[ProgressRead] = ProgressRead) -> CommanderObservation:
        """One frame's progress facts come from ``reading``; Core reads the live forge and Git."""
        frame = HeartbeatFrame.read(comms, thread.name, reading())
        pulls = frame.pulls
        states = UnitState.current(comms, thread.name, frame)
        rules = StandingRule.broken(comms, thread.name, observed=states, frame=frame)
        merged = tuple(todo for state in states for todo in state.merged_orders(comms, pulls))
        return cls(thread, merged, rules, states)


@dataclass
class CommanderHeartbeat(ServiceSender, WatchPass[tuple[CommanderObservation, ...]]):
    sender = "commander-heartbeat"
    reviews: dict[ThreadIncarnation, CommanderReview] = field(default_factory=dict)

    @classmethod
    def requested_by(cls, thread: Thread, goals: Goals) -> tuple[OwnerRequest, ...]:
        """The oldest open request makes a live holder owed service, even before its first beat."""
        return (goals.todos.open_requests(thread.incarnation, limit=1, descending=False)
                if thread.process_alive else ())

    @classmethod
    def commanders(cls, registry: Registration, goals: Goals) -> tuple[Thread, ...]:
        """The live request holders whose current incarnation owns commander work."""
        return tuple(
            thread
            for thread in registry.all_threads().values()
            if cls.requested_by(thread, goals)
        )

    def observe(self, comms: Comms) -> tuple[CommanderObservation, ...]:
        return tuple(
            CommanderObservation.read(comms, thread)
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
            review.wake(comms, self.sender, item.thread, item.rules, item.states)
