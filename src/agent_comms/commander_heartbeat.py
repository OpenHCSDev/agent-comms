"""Service-owned commander review: changed standing rules wake through the real inbox."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from agent_comms.commander_hud import CodexUsage, CommanderFrame, CommanderHud, HudUnavailableGroup
from agent_comms.child_process import ReadWork
from agent_comms.task_attachments.commander_pings import (
    AlarmGroup, CommanderUpdate, CommanderUpdateStore, PublishedFacts, RulePing, UnitPing,
)
from agent_comms.commander_service import CommanderService
from agent_comms.heartbeat_frame import CommanderInputs, HeartbeatFrame
from agent_comms.heartbeat_subjects import CommanderHistory
from agent_comms.message_bus import PreparedWatchPass
from agent_comms.nominal_census import NominalPlanReadings
from agent_comms.phase_evidence import PlanReadings, RetainedPlanReadings
from agent_comms.service_passes import CommanderPass, ServicePassStore
from agent_comms.standing_rules import ServicePassFailing, StandingRule
from agent_comms.threads import Thread
from agent_comms.todos import ProgressRead, Todo
from agent_comms.ui_model.unit_state import UnitState

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


class CommanderHeartbeat(ReadWork, RetainedPlanReadings, NominalPlanReadings, CommanderService,
                         CommanderPass,
                         PreparedWatchPass[Mapping[Thread, CommanderInputs], tuple[CommanderObservation, ...]]):

    def failed(self, opened_wire: Callable[[], AbstractContextManager[Comms]],
               error: Exception) -> None:
        """The service loop has already recorded the failure; every served commander's board
        gains that retained occurrence once, and every line it already holds keeps its age."""
        with opened_wire() as comms:
            name = type(self).__name__
            retained = ServicePassStore.at(comms.root).read().failing[name]
            failure = ServicePassFailing(watch=name, since=retained.since, error=retained.error,
                                         failed_at=retained.failed_at)
            now = time.time()
            snapshot = comms.registry.snapshot()
            actions = CommanderHistory.read(comms, self.actors(snapshot))
            for thread in self.served(comms.registry, comms.goals):
                for update in CommanderUpdate.report_failure(
                    thread.incarnation, failure,
                    CommanderUpdate.latest(comms, thread.incarnation),
                    published=CommanderUpdate.published(comms, thread.incarnation), now=now,
                ):
                    CommanderUpdateStore.record(comms.root, update)
                    sections = AlarmGroup.sections((update,), update.open_pings(actions))
                    update.send(comms, (
                        HudUnavailableGroup.heading(),
                        *CommanderHud.alarm_text(sections, now),
                    ))

    def prepare(self, comms: Comms) -> Mapping[Thread, CommanderInputs]:
        pulls = ProgressRead()
        return {thread: CommanderInputs.prepare(comms, thread.name, pulls, self)
                for thread in self.boards(comms.registry, comms.goals)}

    def observe(self, comms: Comms, prepared: Mapping[Thread, CommanderInputs]) -> tuple[CommanderObservation, ...]:
        return tuple(
            CommanderObservation.read(comms, thread, inputs, plans=self)
            for thread, inputs in prepared.items()
        )

    def pass_body(self, comms: Comms, observed: tuple[CommanderObservation, ...]) -> None:
        """Retain one shared observation per recipient before optional publication."""
        huds = {}
        completed = []
        now = time.time()
        try:
            for item in observed:
                comms.goals.todos.record_merges(item.merges(comms))
                recipients = self.recipients(comms.registry.snapshot(), item.thread)
                held = {recipient.incarnation: CommanderUpdate.latest(comms, recipient.incarnation)
                        for recipient in recipients}
                announced = {
                    recipient.incarnation: CommanderUpdate.published(comms, recipient.incarnation)
                    for recipient in recipients
                }
                previous = held[item.thread.incarnation]
                known = frozenset(ping for packet in previous for ping in packet.handled)
                rejected = StandingRule.enforce_all(
                    comms, item.thread.name,
                    (*(rule for rule in item.rules if RulePing.capture(rule) not in known),
                     *(state for state in item.states
                       if state.attention() and UnitPing.capture(state) not in known)),
                )
                usage = CodexUsage.read()
                # Include acknowledgements committed during forge/provider reads and enforcement.
                history = CommanderHistory.read(comms, self.actors(comms.registry.snapshot()))
                states = tuple(state for state in item.states if state not in rejected)
                rules = tuple(rule for rule in item.rules if rule not in rejected)
                not_applied = tuple(rule.rejected_line(error) for rule, error in rejected.items())
                deliveries = tuple(CommanderUpdate.derive(
                    recipient, states, rules, latest, published=announced[recipient],
                    now=now, history=history,
                    not_applied=not_applied,
                ) for recipient, latest in held.items())
                board = CommanderHud.of(comms, item.thread.name, item.frame, item.states,
                                        delivery=deliveries[0], usage=usage)
                for delivery in deliveries:
                    hud = board.for_delivery(delivery)
                    recipient = delivery.packet.commander
                    CommanderUpdateStore.record(comms.root, delivery.packet)
                    huds[recipient] = hud
                    facts = PublishedFacts.of(hud.display_facts)
                    latest = announced[recipient]
                    if delivery.packet.should_publish(facts, latest):
                        delivery.packet.send(comms, hud.hud_text(now), publication=facts)
                    completed.append(recipient)
        finally:
            # A later commander's failure does not erase frames/passes already completed.
            CommanderFrame.replace_all(comms.root, tuple(huds.values()))
            ServicePassStore.at(comms.root).heartbeat_passed(
                completed,
                (thread.incarnation for thread in comms.registry.all_threads().values()),
            )
