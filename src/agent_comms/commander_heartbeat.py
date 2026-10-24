"""Service-owned commander review: changed standing rules wake through the real inbox."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.commander_hud import CommanderHud
from agent_comms.commander_pings import CommanderUpdate, Ping, RulePing, UnitPing
from agent_comms.commander_service import CommanderService
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.message_bus import WatchPass
from agent_comms.service_passes import ServicePassStore
from agent_comms.standing_rules import StandingRule
from agent_comms.threads import Thread
from agent_comms.todos import ProgressRead
from agent_comms.ui_model.unit_state import UnitState

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.todos import Todo


@dataclass(frozen=True)
class CommanderObservation:
    thread: Thread
    merged: tuple[Todo, ...]
    rules: tuple[StandingRule, ...]
    states: tuple[UnitState, ...]
    hud: tuple[Ping, ...]

    @classmethod
    def read(cls, comms: Comms, thread: Thread,
             reading: type[ProgressRead] = ProgressRead) -> CommanderObservation:
        """One frame's progress facts come from ``reading``; Core reads the live forge and Git."""
        frame = HeartbeatFrame.read(comms, thread.name, reading())
        pulls = frame.pulls
        for repo in comms.registry.snapshot().commander_project.repository_names():
            pulls.open(repo)
        states = UnitState.current(comms, thread.name, frame)
        rules = StandingRule.broken(comms, thread.name, observed=states, frame=frame)
        # An unreadable holder's checkout already has its card; its checks cannot run there.
        unreadable = ({state.unit for state in states}
                      - {unit for state in states for unit in state.readable_units()})
        merged = tuple(
            closed
            for todo in comms.goals.todos.for_commander(thread.incarnation)
            if not any(held.owner_identity in unreadable
                       for held in todo.holder_state.current_assignments())
            for closed in todo.order.closed_by_merge(
                todo, pulls, comms.registry.storage_rule.checkout_base)
        )
        return cls(
            thread, merged, rules, states, CommanderHud.of(comms, thread.name, frame).pings()
        )


class CommanderHeartbeat(CommanderService, WatchPass[tuple[CommanderObservation, ...]]):

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
                    hud=item.hud,
                    not_applied=tuple(
                        rule.rejected_line(error) for rule, error in rejected.items()),
                )
                if update is not None:
                    update.send(comms, previous)
                completed.append(item.thread.incarnation)
        finally:
            # A later commander's failure does not erase passes already completed.
            ServicePassStore.at(comms.root).heartbeat_passed(
                completed,
                (thread.incarnation for thread in comms.registry.all_threads().values()),
            )
