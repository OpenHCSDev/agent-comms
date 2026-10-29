"""Service-owned commander review: changed standing rules wake through the real inbox."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agent_comms.commander_hud import CommanderFrame, CommanderHud, HudUnavailableGroup
from agent_comms.task_attachments.commander_pings import AlarmGroup, CommanderUpdate, RulePing, UnitPing
from agent_comms.commander_service import CommanderService
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.heartbeat_subjects import CommanderHistory
from agent_comms.message_bus import WatchPass
from agent_comms.phase_evidence import PlanReadings, RetainedPlanReadings
from agent_comms.service_passes import ServicePassStore
from agent_comms.standing_rules import ServicePassFailing, StandingRule
from agent_comms.threads import Thread
from agent_comms.todos import ProgressRead, Todo
from agent_comms.ui_model.unit_state import AwaitingCommander, UnitState

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True)
class CommanderObservation:
    thread: Thread
    merged: tuple[Todo, ...]
    rules: tuple[StandingRule, ...]
    states: tuple[UnitState, ...]
    hud: CommanderHud

    @classmethod
    def read(cls, comms: Comms, thread: Thread,
             reading: type[ProgressRead] = ProgressRead, *,
             plans: PlanReadings = PlanReadings()) -> CommanderObservation:
        """One frame's progress facts come from ``reading``; Core reads the live forge and Git."""
        frame = HeartbeatFrame.capture(comms, thread.name, reading(), plans)
        pulls = frame.pulls
        for repo in comms.registry.snapshot().commander_project.repository_names():
            pulls.carry_reviews(comms.goals, repo)
        states = UnitState.current(comms, thread.name, frame)
        rules = StandingRule.broken(comms, thread.name, observed=states, frame=frame)
        # An unreadable holder's checkout already has its card; its checks cannot run there.
        unreadable = ({state.unit for state in states}
                      - {unit for state in states for unit in state.readable_units()})
        merged = tuple(
            closed
            for todo in comms.goals.todos.for_commander(thread.incarnation)
            if not any(held.owner_identity in unreadable
                       for held in todo.current_holder.current_assignments())
            for closed in todo.order.closed_by_merge(
                todo, pulls, comms.registry.storage_rule.checkout_base)
        )
        hud = CommanderHud.of(comms, thread.name, frame, states)
        return cls(thread, merged, rules, states, hud)


class CommanderHeartbeat(RetainedPlanReadings, CommanderService,
                         WatchPass[tuple[CommanderObservation, ...]]):

    def failed(self, opened_wire: Callable[[], AbstractContextManager[Comms]],
               error: Exception) -> None:
        """The service loop has already recorded the failure; each commander's board gains that
        retained occurrence once, and every line it already holds keeps its age."""
        with opened_wire() as comms:
            self.register(comms.registry)
            name = type(self).__name__
            retained = ServicePassStore.at(comms.root).read().failing[name]
            failure = ServicePassFailing(watch=name, since=retained.since, error=retained.error)
            now = time.time()
            for thread in self.commanders(comms.registry, comms.goals):
                for update in CommanderUpdate.report_failure(
                    thread.incarnation, failure,
                    CommanderUpdate.latest(comms, thread.incarnation), now=now,
                ):
                    actions = CommanderHistory.read(comms, self.actors(comms.registry.snapshot(), thread.name))
                    sections = AlarmGroup.sections((update,), update.open_pings(actions))
                    update.send(comms, now, (
                        HudUnavailableGroup.heading(),
                        *CommanderHud.alarm_text(sections, now),
                    ))

    def observe(self, comms: Comms) -> tuple[CommanderObservation, ...]:
        return tuple(
            CommanderObservation.read(comms, thread, plans=self)
            for thread in self.commanders(comms.registry, comms.goals)
        )

    def pass_body(self, comms: Comms, observed: tuple[CommanderObservation, ...]) -> None:
        """The committed packet survives connector replacement; local memory owns nothing."""
        self.register(comms.registry)
        published = {item.thread.incarnation: item.hud for item in observed}
        completed = []
        now = time.time()
        try:
            for item in observed:
                comms.goals.todos.record_merges(item.merged)
                previous = item.hud.packets
                known = frozenset(ping for packet in previous for ping in packet.handled)
                rejected = StandingRule.enforce_all(
                    comms, item.thread.name,
                    (*(rule for rule in item.rules if RulePing.capture(rule) not in known),
                     *(state for state in item.states
                       if state.attention() and UnitPing.capture(state) not in known)),
                )
                # Observations may have waited on forge/provider reads. Acknowledgements
                # committed during those reads belong to this publication, not the old capture.
                actions = CommanderHistory.read(comms, self.actors(comms.registry.snapshot(), item.thread.name))
                for delivery in CommanderUpdate.derive(
                    item.thread.incarnation,
                    tuple(state for state in item.states if state not in rejected),
                    tuple(rule for rule in item.rules if rule not in rejected), previous,
                    now=now, history=actions,
                    undisposed=tuple(
                        ping for packet in previous
                        for ping in packet.undisposed(actions, now - AwaitingCommander.seconds)),
                    not_applied=tuple(
                        rule.rejected_line(error) for rule, error in rejected.items()),
                ):
                    hud = replace(item.hud, packets=(delivery.packet,), open_pings=delivery.open_pings)
                    published[item.thread.incarnation] = hud
                    changed = CommanderFrame.at(comms.root, hud.commander).changed(hud)
                    if delivery.packet.should_publish(changed):
                        delivery.packet.send(comms, now, hud.hud_text(now))
                completed.append(item.thread.incarnation)
        finally:
            CommanderFrame.replace_all(comms.root, tuple(published.values()))
            # A later commander's failure does not erase passes already completed.
            ServicePassStore.at(comms.root).heartbeat_passed(
                completed,
                (thread.incarnation for thread in comms.registry.all_threads().values()),
            )
