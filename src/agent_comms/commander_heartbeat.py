"""Core's commander wake delivers only new, typed decisions, at most five lines."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.commander_pings import (
    CommanderPingMessage, DeliveredPing, LatestCommanderPing, OrderRevision,
    Ping,
)
from agent_comms.message_bus import WatchPass
from agent_comms.message_reference import MessageReference
from agent_comms.registration import Registration
from agent_comms.service_sender import ServiceSender
from agent_comms.standing_rules import StandingRule
from agent_comms.threads import Thread
from agent_comms.todos import CommanderCursor, ListedPullRequest, PullRequests, Todo
from agent_comms.ui_model.unit_state import Moving, UnitState

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.goal_management import Goals


@dataclass(frozen=True)
class CommanderObservation:
    thread: Thread
    merged: tuple[Todo, ...]
    rules: tuple[StandingRule, ...]
    pings: tuple[Ping, ...]
    known: tuple[DeliveredPing, ...]
    cursor: MessageReference | None

    @classmethod
    def read(cls, comms: Comms, sender: str, thread: Thread) -> CommanderObservation:
        held = comms.goals.todos.held_for_commander(thread.incarnation)
        pulls = PullRequests(held)
        # The PR watch and merge reconciliation consume the same forge read.
        merged = comms.goals.todos.merged_orders(thread.incarnation, comms.registry, pulls)
        rules = StandingRule.broken(comms, thread.name)
        states = {state.unit: state for rule in rules for state in rule.unit_states()}
        todos = {todo.id: todo for todo in held}
        previous = LatestCommanderPing.read(comms, sender, thread.incarnation)
        known = previous.delivered(comms) if previous is not None else ()
        candidates = [ping for rule in rules for ping in rule.commander_pings(todos, states)]
        for todo in held:
            state = type(states[todo.held().owner_identity]) if todo.held().owner_identity in states else Moving
            candidates.extend(ping for strike in todo.strikes for ping in strike.commander_pings(todo, state))
            candidates.extend(ping for request in pulls.listed(todo.repo, ListedPullRequest, todo)
                              for ping in request.commander_pings(todo, state))
        # Installation removes the open assignment. The last delivered order reference
        # leads directly to that row, without scanning closed history or mirroring owners.
        for item in known:
            for order in item.ping.orders:
                current = comms.goals.todos.get(order.problem)
                if current.order.checked().id == order.order:
                    candidates.extend(current.order.installation_pings(item.ping, current))
        fresh = tuple(dict.fromkeys(ping for ping in candidates if ping.new_since(known)))
        urgency = UnitState.worst_first()
        selected = tuple(sorted(fresh, key=lambda ping: urgency.index(ping.state)))[:5]
        active = {OrderRevision.of(todo).key for todo in held}
        relevant = active | {order.key for item in known
            if any(order.key in active for order in item.ping.orders) for order in item.ping.orders}
        relevant.update(order.key for ping in fresh if ping not in selected for order in ping.orders)
        kept = tuple(item for item in known
                     if any(order.key in relevant for order in item.ping.orders))
        return cls(thread, merged, rules, selected, kept,
                   previous.reference if previous is not None else None)


class CommanderHeartbeat(ServiceSender, WatchPass[tuple[CommanderObservation, ...]]):
    sender = "commander-heartbeat"

    @classmethod
    def commanders(cls, registry: Registration, goals: Goals) -> tuple[Thread, ...]:
        return tuple(thread for thread in registry.all_threads().values()
                     if thread.process_alive and any(
                         goal.order.open_requests(goal, goals, thread.incarnation) for goal in thread.goal))

    def observe(self, comms: Comms) -> tuple[CommanderObservation, ...]:
        return tuple(CommanderObservation.read(comms, self.sender, thread)
                     for thread in self.commanders(comms.registry, comms.goals))

    def pass_body(self, comms: Comms, observed: tuple[CommanderObservation, ...]) -> None:
        self.register(comms.registry)
        for item in observed:
            comms.goals.todos.record_merges(item.merged)
            StandingRule.enforce_all(comms, item.thread.name, item.rules)
            if item.pings:
                CommanderPingMessage.send(comms, self.sender, item.thread.incarnation,
                                          item.pings, item.known)
            elif item.cursor is not None:
                CommanderCursor(item.thread.incarnation, item.cursor).advance(comms.goals.todos)
