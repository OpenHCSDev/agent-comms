"""Service-owned teammate review: changed standing rules wake through the real inbox."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace

from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.comms import Comms
from agent_comms.coordination.service_passes import ServicePassStore, TeamPass
from agent_comms.execution.child_process import ReadWork
from agent_comms.execution.watch_passes import PreparedWatchPass
from agent_comms.heartbeat_subjects import TeamHistory
from agent_comms.planning.nominal_census import NominalPlanReadings
from agent_comms.planning.orders import ProgressRead, Todo
from agent_comms.projects.checkouts import RepositoryReads
from agent_comms.planning.plan_reading import PlanReadings, RetainedPlanReadings
from agent_comms.storage.watch_phase import WatchPhase
from agent_comms.task_attachments.team_pings import (
    AlarmGroup,
    PublishedFacts,
    RulePing,
    TeammatePing,
    TeamPings,
    TeamUpdate,
)
from agent_comms.team.standing_rules import (
    HeartbeatFrame,
    ServicePassFailing,
    StandingRule,
    TeamInputs,
)
from agent_comms.team.team_hud import TeamBoardPublicationStore, TeamHud, TeamPlan
from agent_comms.team.team_service import TeamService
from agent_comms.ui_model.hud_groups import HudUnavailableGroup
from agent_comms.ui_model.teammate_state import TeammateState


@dataclass(frozen=True)
class TeamObservation:
    """One beat's single team board: every teammate reads the same frame, rules and cards."""

    frame: HeartbeatFrame = field(compare=False)
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


class TeamBoardAcquisition(
    ReadWork,
    RetainedPlanReadings,
    NominalPlanReadings,
    TeamService,
    WatchPhase,
):
    """Acquire and observe one board cut; heartbeat effects live separately."""

    _previous_reads: RepositoryReads | None = None

    def _progress_read(self) -> ProgressRead:
        progress = (
            ProgressRead.retaining(self._previous_reads)
            if self._previous_reads is not None
            else ProgressRead()
        )
        self._previous_reads = progress
        return progress

    def acquire_board(self, comms: Comms) -> tuple[TeamInputs, ...]:
        self.begin_pass()
        progress = self._progress_read()
        return tuple(
            TeamInputs.prepare(comms, project, progress, self)
            for project in comms.registry.snapshot().team_project.boards()
        )

    def observe(
        self,
        comms: Comms,
        prepared: tuple[TeamInputs, ...],
    ) -> tuple[TeamObservation, ...]:
        observed = tuple(
            TeamObservation.read(comms, inputs, plans=self) for inputs in prepared
        )
        self.finish_pass()
        return observed


class TeamHeartbeatEffects(TeamService):
    """Apply one acquired board as the heartbeat's retained/public effects."""

    def prepare_effects(self, comms: Comms, inputs: tuple[TeamInputs, ...]) -> None:
        repositories = comms.registry.snapshot().team_project.repository_names()
        for board in inputs:
            for repository in repositories:
                board.pulls.carry_reviews(comms.goals, repository)

    def pass_body(
        self,
        comms: Comms,
        boards: tuple[TeamObservation, ...],
    ) -> tuple[TeamPlan, tuple[TeamHud, ...]]:
        huds = {}
        plan = TeamPlan()
        completed = []
        now = time.time()
        duty = self.duty(comms.registry, comms.goals)
        retained_store = TeamBoardPublicationStore.at(comms.root)
        retained_packets = retained_store.packet_map(retained_store.read())
        try:
            for observed in boards:
                recipients = self.served(comms.registry, comms.goals)
                held = {
                    recipient.incarnation: retained_packets.get(recipient.incarnation, ())
                    for recipient in recipients
                }
                announced = {
                    recipient.incarnation: TeamUpdate.published(
                        comms, recipient.incarnation
                    )
                    for recipient in recipients
                }
                known = (
                    frozenset.intersection(*(
                        frozenset(
                            ping for packet in packets for ping in packet.handled
                        )
                        for packets in held.values()
                    ))
                    if held
                    else frozenset()
                )
                rejected = StandingRule.enforce_all(
                    comms,
                    duty.actionable((
                        *(
                            rule
                            for rule in observed.rules
                            if RulePing.capture(rule) not in known
                        ),
                        *(
                            state
                            for state in observed.states
                            if state.attention()
                            and TeammatePing.capture(state) not in known
                        ),
                    )),
                )
                usage = CodexConnection.usage()
                history = TeamHistory.of_team(comms)
                states = tuple(
                    state for state in observed.states if state not in rejected
                )
                rules = tuple(rule for rule in observed.rules if rule not in rejected)
                not_applied = tuple(
                    rule.rejected_line(error)
                    for rule, error in rejected.items()
                )
                retained = tuple(
                    packet for packets in held.values() for packet in packets
                )
                if not held:
                    continue
                pings = TeamPings(
                    states,
                    rules,
                    retained,
                    now=now,
                    history=history,
                    not_applied=not_applied,
                )
                deliveries = tuple(
                    pings.delivery(
                        recipient.incarnation,
                        published=announced[recipient.incarnation],
                        delivery=recipient.heartbeat_delivery,
                    )
                    for recipient in recipients
                )
                if not deliveries:
                    continue
                board = TeamHud.of(
                    comms,
                    observed.frame,
                    observed.states,
                    delivery=deliveries[0],
                    usage=usage,
                )
                plan = TeamPlan(board.phases)
                for delivery in deliveries:
                    hud = board.for_delivery(delivery)
                    recipient = delivery.packet.teammate
                    huds[recipient] = hud
                    facts = PublishedFacts.of(hud.display_facts)
                    if delivery.packet.should_publish(
                        facts, announced[recipient]
                    ):
                        duty.publish(
                            lambda delivery=delivery, hud=hud, facts=facts: (
                                delivery.packet.send(
                                    comms,
                                    hud.hud_text(now),
                                    publication=facts,
                                )
                            )
                        )
                    completed.append(recipient)
                comms.goals.todos.record_merges(observed.merges(comms))
            TeamBoardPublicationStore.replace_all(
                comms.root, plan, tuple(huds.values())
            )
        except BaseException:
            TeamBoardPublicationStore.write_all(comms.root, tuple(huds.values()))
            raise
        finally:
            ServicePassStore.at(comms.root).heartbeat_passed(
                tuple(completed),
                (
                    thread.incarnation
                    for thread in comms.registry.all_threads().values()
                ),
            )
        return plan, tuple(huds.values())


class TeamHeartbeat(
    TeamBoardAcquisition,
    TeamHeartbeatEffects,
    TeamPass,
    PreparedWatchPass[tuple[TeamInputs, ...], tuple[TeamObservation, ...]],
    declared_name="team_heartbeat",
):
    """The scheduled owner of board acquisition, effects and publication."""

    def prepare(self, comms: Comms) -> tuple[TeamInputs, ...]:
        inputs = self.acquire_board(comms)
        self.prepare_effects(comms, inputs)
        return inputs

    def failed(
        self,
        opened_wire: Callable[[], AbstractContextManager[Comms]],
        error: Exception,
    ) -> None:
        """Publish one retained service failure to every served teammate."""
        with opened_wire() as comms:
            name = type(self).__name__
            retained = ServicePassStore.at(comms.root).read().failing[name]
            failure = ServicePassFailing(
                watch=name,
                since=retained.since,
                error=retained.error,
                failed_at=retained.failed_at,
            )
            now = time.time()
            snapshot = comms.registry.snapshot()
            actions = TeamHistory.of(comms, snapshot, self.actors(snapshot))
            duty = self.duty(comms.registry, comms.goals)
            publication_store = TeamBoardPublicationStore.at(comms.root)
            publication = publication_store.read()
            retained_huds = {
                hud.teammate: hud
                for hud in publication_store.huds(publication=publication)
            }
            changed_huds = []
            publications = []
            for thread in self.served(comms.registry, comms.goals):
                update = TeamUpdate.report_failure(
                    thread.incarnation,
                    failure,
                    TeamUpdate.latest(comms, thread.incarnation),
                    published=TeamUpdate.published(comms, thread.incarnation),
                    now=now,
                )
                if update is None:
                    continue
                sections = AlarmGroup.sections(
                    (update,),
                    update.open_pings(actions),
                    update.stopped(snapshot),
                )
                current = retained_huds.get(thread.incarnation)
                if current is not None:
                    changed_huds.append(replace(
                        current,
                        packets=(update,),
                        open_pings=update.open_pings(actions),
                        stopped=update.stopped(snapshot),
                    ))
                publications.append((update, sections))
            TeamBoardPublicationStore.write_all(comms.root, tuple(changed_huds))
            for update, sections in publications:
                duty.publish(
                    lambda update=update, sections=sections: update.send(
                        comms,
                        (
                            HudUnavailableGroup.heading(),
                            *TeamHud.alarm_text(sections, now),
                        ),
                    )
                )
