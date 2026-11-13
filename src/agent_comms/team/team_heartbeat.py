"""Service-owned teammate review: changed standing rules wake through the real inbox."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass

from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.comms import Comms
from agent_comms.coordination.service_passes import ServicePassStore, TeamPass
from agent_comms.execution.child_process import ReadWork
from agent_comms.execution.watch_passes import PreparedWatchPass
from agent_comms.heartbeat_subjects import TeamHistory
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.planning.nominal_census import NominalPlanReadings
from agent_comms.planning.orders import ProgressRead, Todo
from agent_comms.planning.plan_reading import PlanReadings, RetainedPlanReadings
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.storage.watch_phase import WatchPhase
from agent_comms.task_attachments.team_pings import (
    AlarmGroup,
    PublishedFacts,
    RulePing,
    TeammatePing,
    TeamPings,
    TeamUpdate,
    TeamUpdateStore,
)
from agent_comms.team.standing_rules import (
    HeartbeatFrame,
    ServicePassFailing,
    StandingRule,
    TeamInputs,
)
from agent_comms.team.team_hud import TeamFrame, TeamHud, TeamPlan
from agent_comms.team.team_service import TeamService
from agent_comms.ui_model.hud_groups import HudUnavailableGroup
from agent_comms.ui_model.teammate_state import TeammateState


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


class BoardRetention(DeclaredFamily, affix="Retention"):
    """What one derived board is allowed to do with itself.

    The board is derived the same way for every reader; only what happens next differs. A
    heartbeat pass enforces the team's standing rules and publishes packets, because it is the
    service's own pass. A view refreshing the board it displays owns no such obligation: it
    derives and retains, and nothing else. Keeping the difference in a member is what lets one
    reader rebuild its board without running the service on a keystroke's behalf.
    """

    def enforced(self, comms: Comms, duty: TeamService, observed: TeamObservation,
                 known: frozenset) -> Mapping[StandingRule, Exception]:
        return {}

    def published(self, comms: Comms, duty: TeamService, packet: TeamUpdate, hud: TeamHud,
                  now: float, latest: PublishedFacts) -> None:
        """A read never publishes service packets."""

    def prepare(self, comms: Comms, inputs: tuple[TeamInputs, ...]) -> None:
        """A display acquires inputs without carrying forge verdicts."""

    def reconcile(self, comms: Comms, observed: TeamObservation) -> None:
        """A display does not run merge checks or close orders."""

    def record(self, comms: Comms, packet: TeamUpdate) -> None:
        """A display retains frames, never service delivery records."""

    def retain(self, comms: Comms, plan: TeamPlan, huds: tuple[TeamHud, ...]) -> None:
        """A preparation returns its result without publishing from a droppable read."""

    def keep(self, comms: Comms, huds: tuple[TeamHud, ...]) -> None:
        """A pass that threw keeps the boards it finished; a display keeps nothing."""

    def completed(self, comms: Comms, teammates: tuple[ThreadIncarnation, ...]) -> None:
        """A read never advances the service completion clock."""


@dataclass(frozen=True)
class HeartbeatRetention(BoardRetention):
    """The service's pass: it enforces the rules and publishes every teammate's packet."""

    def enforced(self, comms, duty, observed, known):
        return StandingRule.enforce_all(comms, duty.actionable(
            (*(rule for rule in observed.rules if RulePing.capture(rule) not in known),
             *(state for state in observed.states
               if state.attention() and TeammatePing.capture(state) not in known))))

    def published(self, comms, duty, packet, hud, now, latest):
        facts = PublishedFacts.of(hud.display_facts)
        if packet.should_publish(facts, latest):
            duty.publish(lambda: packet.send(comms, hud.hud_text(now), publication=facts))

    def prepare(self, comms, inputs):
        # Asked of the project, not of `require()`: a bus with no registered project answers
        # no repositories and has no boards to carry reviews for, while requiring one raised
        # on every pass and logged a failed service worker (test_connector's request log).
        repositories = comms.registry.snapshot().team_project.repository_names()
        for board in inputs:
            for repository in repositories:
                board.pulls.carry_reviews(comms.goals, repository)

    def reconcile(self, comms, observed):
        comms.goals.todos.record_merges(observed.merges(comms))

    def record(self, comms, packet):
        TeamUpdateStore.record(comms.root, packet)

    def retain(self, comms, plan, huds):
        TeamFrame.replace_all(comms.root, plan, huds)

    def keep(self, comms, huds):
        TeamFrame.write_all(comms.root, huds)

    def completed(self, comms, teammates):
        ServicePassStore.at(comms.root).heartbeat_passed(
            teammates,
            (thread.incarnation for thread in comms.registry.all_threads().values()),
        )


@dataclass(frozen=True)
class ReadRetention(BoardRetention):
    """A view prepares the shared board with no service effects."""


class TeamBoardRead(ReadWork, RetainedPlanReadings, NominalPlanReadings, TeamService, WatchPhase):
    """The shared board derivation, independent of the heartbeat's scheduling and effects."""

    def read_board(self, comms: Comms) -> tuple[TeamPlan, tuple[TeamHud, ...]]:
        """Prepare the shared board without service effects or retained-frame writes."""
        retention = ReadRetention()
        return self.pass_body(
            comms, self.observe(comms, self.prepare_board(comms, retention)), retention)

    def prepare_board(self, comms: Comms, retention: BoardRetention) -> tuple[TeamInputs, ...]:
        """Acquire one shared board; only the service carries its forge verdicts."""
        self.begin_pass()
        inputs = tuple(TeamInputs.prepare(comms, project, ProgressRead(), self)
                       for project in comms.registry.snapshot().team_project.boards())
        retention.prepare(comms, inputs)
        return inputs

    def observe(self, comms: Comms,
                prepared: tuple[TeamInputs, ...]) -> tuple[TeamObservation, ...]:
        observed = tuple(TeamObservation.read(comms, inputs, plans=self) for inputs in prepared)
        self.finish_pass()
        return observed

    def pass_body(self, comms: Comms, boards: tuple[TeamObservation, ...],
                  retention: BoardRetention = HeartbeatRetention(),
                  ) -> tuple[TeamPlan, tuple[TeamHud, ...]]:
        """Derive the team board once, then let ``retention`` say what happens to it."""
        huds = {}
        plan = TeamPlan()
        completed = []
        now = time.time()
        duty = self.duty(comms.registry, comms.goals)
        try:
            for observed in boards:
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
                rejected = retention.enforced(comms, duty, observed, known)
                usage = CodexConnection.usage()
                # Include acknowledgements committed during forge/provider reads and enforcement.
                history = TeamHistory.of_team(comms)
                states = tuple(state for state in observed.states if state not in rejected)
                rules = tuple(rule for rule in observed.rules if rule not in rejected)
                not_applied = tuple(rule.rejected_line(error) for rule, error in rejected.items())
                # One board: every teammate's retained packet dates its lines.
                retained = tuple(packet for packets in held.values() for packet in packets)
                if not held:
                    continue  # No live teammate: enforced, and nobody to write a board for.
                pings = TeamPings(states, rules, retained, now=now, history=history,
                                  not_applied=not_applied)
                deliveries = tuple(pings.delivery(recipient, published=announced[recipient])
                                   for recipient in held)
                board = TeamHud.of(comms, observed.frame, observed.states,
                                   delivery=deliveries[0], usage=usage)
                plan = TeamPlan(board.phases)
                for delivery in deliveries:
                    hud = board.for_delivery(delivery)
                    recipient = delivery.packet.teammate
                    retention.record(comms, delivery.packet)
                    huds[recipient] = hud
                    retention.published(comms, duty, delivery.packet, hud, now,
                                        announced[recipient])
                    completed.append(recipient)
                # After this teammate's board exists, because reconciling reads git: a merge
                # check that cannot read one repo used to throw here with huds still empty.
                retention.reconcile(comms, observed)
            # Only a pass that got through every teammate may replace the team's frames.
            # TeamFrame.replace_all unlinks every frame absent from huds, which is what a
            # teammate that stopped beating deserves and the opposite of what a pass that
            # threw deserves: on 2026-11-08 one Textual worktree could not read a blob for
            # patch-id, the first reconcile raised with huds empty, and retaining that empty
            # fragment deleted every board. Tristan's read said "frame missing" for 8 hours.
            retention.retain(comms, plan, tuple(huds.values()))
        except BaseException:
            # A pass that threw still keeps every board it finished, and unlinks none: a
            # refused publication rolls back the publication, not the frame it was about.
            retention.keep(comms, tuple(huds.values()))
            raise
        finally:
            # Passes already completed stay completed, whether or not a later one raised.
            retention.completed(comms, tuple(completed))
        return plan, tuple(huds.values())


class TeamHeartbeat(TeamBoardRead, TeamPass,
                    PreparedWatchPass[tuple[TeamInputs, ...], tuple[TeamObservation, ...]],
                    declared_name="team_heartbeat"):
    """The scheduled service adds its effects to the same board the UI prepares."""

    def prepare(self, comms: Comms) -> tuple[TeamInputs, ...]:
        return self.prepare_board(comms, HeartbeatRetention())

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
                if (update := TeamUpdate.report_failure(
                    thread.incarnation, failure,
                    TeamUpdate.latest(comms, thread.incarnation),
                    published=TeamUpdate.published(comms, thread.incarnation), now=now,
                )) is not None:
                    TeamUpdateStore.record(comms.root, update)
                    sections = AlarmGroup.sections((update,), update.open_pings(actions),
                                                   update.stopped(snapshot))
                    duty.publish(lambda update=update, sections=sections: update.send(comms, (
                        HudUnavailableGroup.heading(),
                        *TeamHud.alarm_text(sections, now),
                    )))
