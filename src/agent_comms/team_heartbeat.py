"""Service-owned teammate review: changed standing rules wake through the real inbox."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.team_hud import TeamFrame, TeamHud, TeamPlan

from agent_comms.team_hud_records import HudUnavailableGroup
from agent_comms.child_process import ReadWork
from agent_comms.task_attachments.team_pings import (
    AlarmGroup, TeamUpdate, TeamUpdateStore, PublishedFacts, RulePing, TeammatePing,
)
from agent_comms.team_service import TeamService
from agent_comms.heartbeat_frame import TeamInputs, HeartbeatFrame
from agent_comms.heartbeat_subjects import TeamHistory
from agent_comms.message_bus import PreparedWatchPass
from agent_comms.nominal_census import NominalPlanReadings
from agent_comms.plan_reading import PlanReadings, RetainedPlanReadings
from agent_comms.service_passes import TeamPass, ServicePassStore
from agent_comms.standing_rules import ServicePassFailing, StandingRule
from agent_comms.todos import ProgressRead, Todo
from agent_comms.ui_model.teammate_state import TeammateState

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True)
class TeamObservation:
    """One beat's single team board: every teammate reads the same frame, rules and cards."""

    frame: HeartbeatFrame
    rules: tuple[StandingRule, ...]
    states: tuple[TeammateState, ...]

    @classmethod
    def read(cls, comms: Comms, inputs: TeamInputs, *, plans: PlanReadings) -> TeamObservation:
        """Consume the prepared cut; Core observes the local Git, forge and plan answers."""
        frame = HeartbeatFrame.capture(comms, inputs, plans)
        states = TeammateState.current(comms, frame)
        rules = StandingRule.broken(comms, observed=states, frame=frame)
        return cls(frame, rules, states)

    def merges(self, comms: Comms) -> tuple[Todo, ...]:
        """Merge checks may fetch, prepare checkouts and run commands; never a read phase."""
        # An unreadable holder's checkout already has its card; its checks cannot run there.
        unreadable = ({state.unit for state in self.states}
                      - {unit for state in self.states for unit in state.readable_units()})
        return tuple(
            closed
            for todo in comms.goals.todos.board()
            if not any(held.owner_identity in unreadable
                       for held in todo.current_holder.current_assignments())
            for closed in todo.order.closed_by_merge(
                todo, self.frame.pulls, comms.registry.storage_rule.checkout_base)
        )


class TeamHeartbeat(ReadWork, RetainedPlanReadings, NominalPlanReadings, TeamService,
                         TeamPass,
                         PreparedWatchPass[tuple[TeamInputs, ...], tuple[TeamObservation, ...]],
                         declared_name="team_heartbeat"):

    def failed(self, opened_wire: Callable[[], AbstractContextManager[Comms]],
               error: Exception) -> None:
        """The service loop has already recorded the failure; every served teammate's board
        gains that retained occurrence once, and every line it already holds keeps its age."""
        with opened_wire() as comms:
            name = type(self).__name__
            retained = ServicePassStore.at(comms.root).read().failing[name]
            failure = ServicePassFailing(watch=name, since=retained.since, error=retained.error,
                                         failed_at=retained.failed_at)
            now = time.time()
            snapshot = comms.registry.snapshot()
            actions = TeamHistory.of(comms, snapshot, self.actors(snapshot))
            duty = self.duty(comms.registry, comms.goals)
            for thread in self.served(comms.registry, comms.goals):
                for update in TeamUpdate.report_failure(
                    thread.incarnation, failure,
                    TeamUpdate.latest(comms, thread.incarnation),
                    published=TeamUpdate.published(comms, thread.incarnation), now=now,
                ):
                    TeamUpdateStore.record(comms.root, update)
                    sections = AlarmGroup.sections((update,), update.open_pings(actions),
                                                   update.stopped(snapshot))
                    duty.publish(lambda update=update, sections=sections: update.send(comms, (
                        HudUnavailableGroup.heading(),
                        *TeamHud.alarm_text(sections, now),
                    )))

    def prepare(self, comms: Comms) -> tuple[TeamInputs, ...]:
        """The bus's one team board, or none on a bus with no registered team project."""
        self.begin_pass()
        return tuple(TeamInputs.prepare(comms, project, ProgressRead(), self)
                     for project in comms.registry.snapshot().team_project.boards())

    def observe(self, comms: Comms,
                prepared: tuple[TeamInputs, ...]) -> tuple[TeamObservation, ...]:
        observed = tuple(TeamObservation.read(comms, inputs, plans=self) for inputs in prepared)
        self.finish_pass()
        return observed

    def pass_body(self, comms: Comms, boards: tuple[TeamObservation, ...]) -> None:
        """Enforce the team board once, then retain and publish it per teammate."""
        huds = {}
        plan = TeamPlan()
        completed = []
        now = time.time()
        duty = self.duty(comms.registry, comms.goals)
        try:
            for observed in boards:
                comms.goals.todos.record_merges(observed.merges(comms))
                recipients = self.served(comms.registry, comms.goals)
                held = {recipient.incarnation: TeamUpdate.latest(comms, recipient.incarnation)
                        for recipient in recipients}
                announced = {
                    recipient.incarnation: TeamUpdate.published(comms, recipient.incarnation)
                    for recipient in recipients
                }
                # A line is skipped only once every teammate's own packet handled it: a teammate
                # who joins later still gets its effect, which the effects themselves keep
                # once-only (a burst is sent once per recipient birth, per the bus).
                known = frozenset.intersection(*(
                    frozenset(ping for packet in packets for ping in packet.handled)
                    for packets in held.values())) if held else frozenset()
                rejected = StandingRule.enforce_all(
                    comms, duty.actionable(
                        (*(rule for rule in observed.rules if RulePing.capture(rule) not in known),
                         *(state for state in observed.states
                           if state.attention() and TeammatePing.capture(state) not in known))),
                )
                usage = CodexConnection.usage()
                # Include acknowledgements committed during forge/provider reads and enforcement.
                history = TeamHistory.of_team(comms)
                states = tuple(state for state in observed.states if state not in rejected)
                rules = tuple(rule for rule in observed.rules if rule not in rejected)
                not_applied = tuple(rule.rejected_line(error) for rule, error in rejected.items())
                # One board: every teammate's retained packet dates its lines.
                retained = tuple(packet for packets in held.values() for packet in packets)
                deliveries = tuple(TeamUpdate.derive(
                    recipient, states, rules, retained, published=announced[recipient],
                    now=now, history=history,
                    not_applied=not_applied,
                ) for recipient in held)
                if not deliveries:
                    continue  # No live teammate: enforced, and nobody to write a board for.
                board = TeamHud.of(comms, observed.frame, observed.states,
                                   delivery=deliveries[0], usage=usage)
                plan = TeamPlan(board.phases)
                for delivery in deliveries:
                    hud = board.for_delivery(delivery)
                    recipient = delivery.packet.teammate
                    TeamUpdateStore.record(comms.root, delivery.packet)
                    huds[recipient] = hud
                    facts = PublishedFacts.of(hud.display_facts)
                    latest = announced[recipient]
                    if delivery.packet.should_publish(facts, latest):
                        duty.publish(lambda packet=delivery.packet, hud=hud, facts=facts:
                                     packet.send(comms, hud.hud_text(now), publication=facts))
                    completed.append(recipient)
        finally:
            # A later teammate's failure does not erase frames/passes already completed.
            TeamFrame.replace_all(comms.root, plan, tuple(huds.values()))
            ServicePassStore.at(comms.root).heartbeat_passed(
                completed,
                (thread.incarnation for thread in comms.registry.all_threads().values()),
            )
