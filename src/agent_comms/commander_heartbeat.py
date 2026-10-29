"""Service-owned commander review: changed standing rules wake through the real inbox."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.commander_hud import CommanderFrame, CommanderHud
from agent_comms.child_process import ReadWork
from agent_comms.task_attachments.commander_pings import CommanderUpdate, RulePing, UnitPing
from agent_comms.commander_service import CommanderService
from agent_comms.heartbeat_frame import CommanderInputs, HeartbeatFrame
from agent_comms.message_bus import PreparedWatchPass
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
    frame: HeartbeatFrame
    rules: tuple[StandingRule, ...]
    states: tuple[UnitState, ...]

    @classmethod
    def read(cls, comms: Comms, thread: Thread,
             inputs: CommanderInputs, *,
             plans: PlanReadings) -> CommanderObservation:
        """Consume the prepared cut; Core observes the local Git, forge and plan answers."""
        frame = HeartbeatFrame.capture(comms, thread.name, inputs, plans)
        states = UnitState.current(comms, thread.name, frame)
        rules = StandingRule.broken(comms, thread.name, observed=states, frame=frame)
        return cls(thread, frame, rules, states)

    def merges(self, comms: Comms) -> tuple[Todo, ...]:
        """Merge checks may fetch, prepare checkouts and run commands; never a read phase."""
        # An unreadable holder's checkout already has its card; its checks cannot run there.
        unreadable = ({state.unit for state in self.states}
                      - {unit for state in self.states for unit in state.readable_units()})
        return tuple(
            closed
            for todo in comms.goals.todos.for_commander(self.thread.incarnation)
            if not any(held.owner_identity in unreadable
                       for held in todo.current_holder.current_assignments())
            for closed in todo.order.closed_by_merge(
                todo, self.frame.pulls, comms.registry.storage_rule.checkout_base)
        )


class CommanderHeartbeat(ReadWork, RetainedPlanReadings, CommanderService,
                         PreparedWatchPass[Mapping[Thread, CommanderInputs], tuple[CommanderObservation, ...]]):

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
                    update.send(comms, now, ())

    def prepare(self, comms: Comms) -> Mapping[Thread, CommanderInputs]:
        return {thread: CommanderInputs.prepare(comms, thread.name, ProgressRead(), self)
                for thread in self.commanders(comms.registry, comms.goals)}

    def observe(self, comms: Comms, prepared: Mapping[Thread, CommanderInputs]) -> tuple[CommanderObservation, ...]:
        return tuple(
            CommanderObservation.read(comms, thread, inputs, plans=self)
            for thread, inputs in prepared.items()
        )

    def pass_body(self, comms: Comms, observed: tuple[CommanderObservation, ...]) -> None:
        """The committed packet survives connector replacement; local memory owns nothing."""
        self.register(comms.registry)
        completed = []
        huds = []
        now = time.time()
        try:
            for item in observed:
                comms.goals.todos.record_merges(item.merges(comms))
                previous = CommanderUpdate.latest(comms, item.thread.incarnation)
                known = frozenset(ping for packet in previous for ping in packet.handled)
                rejected = StandingRule.enforce_all(
                    comms, item.thread.name,
                    (*(rule for rule in item.rules if RulePing.capture(rule) not in known),
                     *(state for state in item.states
                       if state.attention() and UnitPing.capture(state) not in known)),
                )
                # Board disposition reads CommanderHistory after enforcement.
                hud = CommanderHud.of(comms, item.thread.name, item.frame, item.states)
                for update in CommanderUpdate.derive(
                    item.thread.incarnation,
                    tuple(state for state in item.states if state not in rejected),
                    tuple(rule for rule in item.rules if rule not in rejected), previous,
                    now=now,
                    undisposed=tuple(
                        ping for packet in previous
                        for ping in packet.undisposed(comms, item.thread.name,
                                                      now - AwaitingCommander.seconds)),
                    not_applied=tuple(
                        rule.rejected_line(error) for rule, error in rejected.items()),
                ):
                    update.send(comms, now, hud.brief())
                huds.append(hud)
                completed.append(item.thread.incarnation)
        finally:
            # A later commander's failure does not erase passes already completed.
            CommanderFrame.replace_all(comms.root, tuple(huds))
            ServicePassStore.at(comms.root).heartbeat_passed(
                completed,
                (thread.incarnation for thread in comms.registry.all_threads().values()),
            )
