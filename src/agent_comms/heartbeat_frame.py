"""What one heartbeat reads from GitHub and the plans, read once and shared by every rule."""

from __future__ import annotations

from collections.abc import Mapping
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from agent_comms.head_review import PullRequestHead
from agent_comms.held_orders import HeldOrders
from agent_comms.phase_evidence import PlanReading, PlanReadings
from agent_comms.plan_phases import PlanPhase
from agent_comms.repo import Repo
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import OwedAnswer, StandingRule
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.storage_rules import EditWorkplaces
from agent_comms.todos import (
    ClosedHeadReading, HeadReviewRecord, ProgressRead, StoredClosedHead, StoredHeadReview,
    Todo, TodoStore,
)

if TYPE_CHECKING:
    from agent_comms.comms import Comms


@dataclass(frozen=True)
class CommanderInputs(HeldOrders):
    """The live hold cut and acquired inputs that observation must not reopen."""

    pulls: ProgressRead
    plan: PlanReading

    @classmethod
    def prepare(cls, comms: Comms, commander: str, pulls: ProgressRead,
                plans: PlanReadings) -> Self:
        holds = HeldOrders.read(comms, commander)
        pulls.register(holds.todos)
        snapshot = comms.registry.snapshot()
        project = snapshot.commander_project.require()
        for repo in project.repository_names():
            pulls.carry_reviews(comms.goals, repo)
        StandingRule.prepare_all(comms, commander, holds, pulls)
        repository = Repo.declaring(project.repos, PlanPhase.__module__)
        plan = plans.prepare_reading(
            repository,
            comms.goals.todos.history_for_commander(snapshot.require(commander).incarnation),
            tuple(phase.phase_evidence() for phase in PlanPhase.members_with(PlanPhase)),
        )
        return cls(holds.live, holds.orphaned, pulls, plan)


@dataclass(frozen=True)
class HeartbeatFrame(CommanderInputs):
    """One beat's observed answers, sharing its already prepared hold/forge/plan inputs."""

    answers: tuple[OwedAnswer, ...]
    closed: ClosedHeadReading
    head_reviews: Mapping[PullRequestHead, tuple[HeadReviewRecord, ...]]
    workplaces: Mapping[ThreadIncarnation, EditWorkplaces]
    review_timers: tuple[tuple[Todo, RecordedEventTime], ...]

    @classmethod
    def capture(cls, comms: Comms, commander: str, inputs: CommanderInputs,
                plans: PlanReadings) -> Self:
        snapshot = comms.registry.snapshot()
        project = snapshot.commander_project.require()
        with comms.goals.todos.snapshot() as db:
            closed = StoredClosedHead.current(db, project.repository_names())
            reviews = StoredHeadReview.head_reviews_among(
                db, (head.request for head in closed.lost))
        plan = plans.observe_reading(inputs.plan,
            tuple(phase.phase_evidence() for phase in PlanPhase.members_with(PlanPhase)))
        return cls(
            inputs.live,
            inputs.orphaned,
            inputs.pulls,
            plan,
            OwedAnswer.read_scan(comms, commander, inputs),
            closed,
            reviews,
            EditWorkplaces.capture(comms, snapshot, inputs.live, inputs.pulls),
            TodoStore.timers_up_in(inputs.todos, time.time(), inputs.pulls.progress(inputs.todos, comms.registry)),
        )
