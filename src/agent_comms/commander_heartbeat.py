"""Service-owned commander review: changed standing rules wake through the real inbox."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.commander_pings import CommanderUpdate, RulePing, UnitPing
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.message_bus import WatchPass
from agent_comms.registration import Registration
from agent_comms.service_sender import ServiceSender
from agent_comms.service_passes import ServicePassStore
from agent_comms.standing_rules import StandingRule
from agent_comms.threads import Thread
from agent_comms.todos import ProgressRead
from agent_comms.ui_model.unit_state import UnitState

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.goal_management import Goals
    from agent_comms.todos import OwnerRequest, Todo


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
        pulls = reading(StandingRule.todos(comms, thread.name))
        for repo in comms.registry.snapshot().commander_project.repository_names():
            pulls.open(repo)
        states = UnitState.current(comms, thread.name, pulls)
        frame = HeartbeatFrame.read(comms, thread.name, pulls)
        rules = StandingRule.broken(comms, thread.name, observed=states, frame=frame)
        merged = tuple(todo for state in states for todo in state.merged_orders(comms, pulls))
        return cls(thread, merged, rules, states)


class CommanderHeartbeat(ServiceSender, WatchPass[tuple[CommanderObservation, ...]]):
    sender = "commander-heartbeat"

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
        """The committed packet survives connector replacement; local memory owns nothing."""
        self.register(comms.registry)
        completed = []
        try:
            for item in observed:
                comms.goals.todos.record_merges(item.merged)
                previous = CommanderUpdate.latest(comms, item.thread.incarnation)
                known = frozenset(previous.handled) if previous else frozenset()
                rejected = StandingRule.enforce_all(
                    comms, item.thread.name,
                    (*(rule for rule in item.rules if RulePing.capture(rule) not in known),
                     *(state for state in item.states
                       if state.attention() and UnitPing.capture(state) not in known)),
                )
                update = CommanderUpdate.derive(
                    item.thread.incarnation,
                    tuple(state for state in item.states if state not in rejected),
                    tuple(rule for rule in item.rules if rule not in rejected), previous,
                )
                if update is not None:
                    update.send(comms, previous)
                completed.append(item.thread.incarnation)
        finally:
            # A later commander's failure does not erase passes already completed.
            ServicePassStore.at(comms.root).heartbeat_passed(
                completed, (item.thread.incarnation for item in observed),
            )
