"""Service-owned teammate review: changed standing rules wake through the real inbox."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from functools import cached_property

from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.comms import Comms
from agent_comms.coordination.service_passes import ServicePassStore, TeamPass
from agent_comms.execution.child_process import ReadWork
from agent_comms.execution.watch_passes import PreparedWatchPass
from agent_comms.heartbeat_subjects import TeamHistory
from agent_comms.messaging.revision_views import OwedAnswerView
from agent_comms.planning.held_orders import HeldOrders
from agent_comms.planning.nominal_census import NominalPlanReadings
from agent_comms.planning.orders import ProgressRead, Todo
from agent_comms.planning.plan_reading import PlanReadings, RetainedPlanReadings
from agent_comms.presentation import WireRevision
from agent_comms.projects.checkouts import RepositoryReads
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.storage.watch_phase import WatchPhase
from agent_comms.task_attachments.team_pings import (
    AlarmGroup,
    AlarmSection,
    RetainedPingCuts,
    RulePing,
    TeammatePing,
    TeamPings,
    TeamUpdate,
)
from agent_comms.team.standing_rules import (
    HeartbeatFrame,
    OwedAnswer,
    ServicePassFailing,
    StandingRule,
    TeamInputs,
)
from agent_comms.team.team_hud import (
    RetainedTeamBoard,
    TeamBoardPublicationStore,
    TeamHud,
    TeamPlan,
)
from agent_comms.team.team_service import TeamService
from agent_comms.ui_model.hud_groups import HudUnavailableGroup
from agent_comms.ui_model.teammate_state import TeammateState


@dataclass(frozen=True)
class OwedAnswerCut:
    """One retained answer-family result and every stored identity that supplied it.

    ``OwedAnswer.due`` and the remaining standing-rule derivation run on every beat.  A source
    revision, selected owner/hold change, or a member-owned source-validity boundary changes
    the cut; otherwise the discovered answer providers keep their exact prior result.
    """

    revision: WireRevision
    owners: tuple[ThreadIncarnation, ...]
    holds: HeldOrders
    answers: tuple[OwedAnswer, ...]
    valid_until: float

    def serves(self, owners: tuple[ThreadIncarnation, ...], inputs: TeamInputs) -> bool:
        return self.owners == owners and self.holds == HeldOrders(inputs.live, inputs.orphaned)

    @classmethod
    def capture(
        cls,
        comms: Comms,
        inputs: TeamInputs,
        owners: tuple[ThreadIncarnation, ...],
        revision: WireRevision,
        previous: OwedAnswerCut | None,
        *,
        now: float | None = None,
    ) -> OwedAnswerCut:
        captured_at = time.time() if now is None else now
        holds = HeldOrders(inputs.live, inputs.orphaned)
        if (
            previous is not None
            and previous.serves(owners, inputs)
            and captured_at < previous.valid_until
            and not revision.changed_since(OwedAnswerView(), previous.revision)
        ):
            return replace(previous, revision=revision)
        answers = tuple({
            answer.observation_key(): answer
            for answer in OwedAnswer.read_scan(comms, owners, inputs)
        }.values())
        valid_until = min(
            (answer.source_valid_until() for answer in answers),
            default=float("inf"),
        )
        return cls(revision, owners, holds, answers, valid_until)


@dataclass(frozen=True)
class TeamObservation:
    """One beat's single team board: every teammate reads the same frame, rules and cards."""

    frame: HeartbeatFrame = field(compare=False)
    rules: tuple[StandingRule, ...]
    states: tuple[TeammateState, ...]

    @cached_property
    def state_pings(self) -> tuple[TeammatePing, ...]:
        return tuple(TeammatePing.capture(state) for state in self.states if state.attention())

    @cached_property
    def rule_pings(self) -> tuple[RulePing, ...]:
        return tuple(RulePing.capture_actions(rule) for rule in self.rules)

    @classmethod
    def read(
        cls,
        comms: Comms,
        inputs: TeamInputs,
        *,
        plans: PlanReadings,
        answers: tuple[OwedAnswer, ...] | None = None,
        snapshot: RegistrySnapshot | None = None,
    ) -> TeamObservation:
        """Consume the prepared cut; Core observes the local Git, forge and plan answers."""
        frame = HeartbeatFrame.capture(
            comms, inputs, plans, answers=answers, snapshot=snapshot,
        )
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
            for todo in self.frame.open_orders
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
    _answer_cuts: tuple[OwedAnswerCut, ...] = ()

    def _progress_read(self) -> ProgressRead:
        progress = (
            ProgressRead.retaining(self._previous_reads)
            if self._previous_reads is not None
            else ProgressRead()
        )
        self._previous_reads = progress
        return progress

    def close(self) -> None:
        if self._previous_reads is not None:
            self._previous_reads.close()
            self._previous_reads = None

    def acquire_board(self, comms: Comms) -> tuple[TeamInputs, ...]:
        self.begin_pass()
        progress = self._progress_read()
        snapshot = comms.registry.snapshot()
        return tuple(
            TeamInputs.prepare(
                comms, project, progress, self, snapshot=snapshot,
            )
            for project in snapshot.team_project.boards()
        )

    def observe(
        self,
        comms: Comms,
        prepared: tuple[TeamInputs, ...],
    ) -> tuple[TeamObservation, ...]:
        revision = comms.views.revision()
        snapshot = prepared[0].snapshot if prepared else comms.registry.snapshot()
        if any(inputs.snapshot is not snapshot for inputs in prepared):
            raise ValueError("One board acquisition must carry one registry cut")
        owners = tuple(
            teammate.incarnation
            for teammate in TeamService.teammates(snapshot, comms.goals)
        )
        cuts: list[OwedAnswerCut] = []
        observed: list[TeamObservation] = []
        for inputs in prepared:
            previous = next(
                (cut for cut in self._answer_cuts if cut.serves(owners, inputs)),
                None,
            )
            cut = OwedAnswerCut.capture(
                comms, inputs, owners, revision, previous,
            )
            cuts.append(cut)
            observed.append(TeamObservation.read(
                comms,
                inputs,
                plans=self,
                answers=cut.answers,
                snapshot=snapshot,
            ))
        self._answer_cuts = tuple(cuts)
        self.finish_pass()
        return tuple(observed)


class TeamHeartbeatEffects(TeamService):
    """Apply one acquired board as the heartbeat's retained/public effects."""

    _retained_board: RetainedTeamBoard | None = None

    def prepare_effects(self, comms: Comms, inputs: tuple[TeamInputs, ...]) -> None:
        if not inputs:
            return
        snapshot = inputs[0].snapshot
        if any(board.snapshot is not snapshot for board in inputs):
            raise ValueError("One board preparation must carry one registry cut")
        repositories = snapshot.team_project.repository_names()
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
        snapshot = boards[0].frame.snapshot if boards else comms.registry.snapshot()
        if any(board.frame.snapshot is not snapshot for board in boards):
            raise ValueError("One heartbeat must carry one registry cut")
        duty = self.duty(comms.registry, comms.goals, snapshot=snapshot)
        if self._retained_board is None:
            self._retained_board = TeamBoardPublicationStore.at(comms.root).retained()
        retained_packets = self._retained_board.packet_map()
        retained_huds = {hud.teammate: hud for hud in self._retained_board.huds}
        try:
            for observed in boards:
                recipients = self.served(
                    comms.registry, comms.goals, snapshot=snapshot,
                )
                held = {
                    recipient.incarnation: retained_packets.get(recipient.incarnation, ())
                    for recipient in recipients
                }
                announced = TeamUpdate.published_among(
                    comms,
                    tuple(recipient.incarnation for recipient in recipients),
                )
                retained = RetainedPingCuts.from_packets(held.values())
                known = retained.common()
                observed_rules = tuple(zip(
                    observed.rules, observed.rule_pings, strict=True,
                ))
                observed_states = tuple(zip(
                    (state for state in observed.states if state.attention()),
                    observed.state_pings,
                    strict=True,
                ))
                rejected = StandingRule.enforce_all(
                    comms,
                    duty.actionable((
                        *(
                            rule
                            for rule, ping in observed_rules
                            if ping not in known
                        ),
                        *(
                            state
                            for state, ping in observed_states
                            if ping not in known
                        ),
                    )),
                )
                usage = CodexConnection.usage()
                if rejected:
                    states = tuple(
                        state for state in observed.states if state not in rejected
                    )
                    rules = tuple(rule for rule in observed.rules if rule not in rejected)
                    captured = (
                        *(ping for state, ping in observed_states if state not in rejected),
                        *(ping for rule, ping in observed_rules if rule not in rejected),
                    )
                else:
                    states = observed.states
                    rules = observed.rules
                    captured = (*observed.state_pings, *observed.rule_pings)
                history = TeamHistory.of(
                    comms, snapshot, TeamService.actors(snapshot),
                )
                not_applied = tuple(
                    rule.rejected_line(error)
                    for rule, error in rejected.items()
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
                    captured=captured,
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
                    hud = board.for_delivery(delivery).retaining_rendering(
                        retained_huds.get(delivery.packet.teammate)
                    )
                    recipient = delivery.packet.teammate
                    huds[recipient] = hud
                    facts = hud.display.publication
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
            self._retained_board = TeamBoardPublicationStore.replace_all(
                comms.root,
                plan,
                tuple(huds.values()),
                self._retained_board,
            )
        except BaseException:
            self._retained_board = None
            TeamBoardPublicationStore.write_all(comms.root, tuple(huds.values()))
            raise
        finally:
            ServicePassStore.at(comms.root).heartbeat_passed(
                tuple(completed),
                (
                    thread.incarnation
                    for thread in snapshot.threads.values()
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
                sections = AlarmSection.bind(
                    AlarmGroup.displays(
                        (update,),
                        update.open_pings(actions),
                        update.stopped(snapshot),
                    ),
                    update.teammate,
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
            self._retained_board = None
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
