"""What one heartbeat reads from GitHub and the plans, read once and shared by every rule."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from agent_comms.commander.publication_rules import ClosedUnmergedPullRequest
from agent_comms.head_review import PullRequestHead
from agent_comms.held_orders import HeldOrders
from agent_comms.phase_evidence import PlanReading, PlanReadings
from agent_comms.plan_phases import PlanPhase
from agent_comms.repo import Repo
from agent_comms.standing_rules import OwedAnswer
from agent_comms.todos import StoredClosedHead, StoredHeadReview

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.todos import HeadReviewRecord, ProgressRead


@dataclass(frozen=True)
class HeartbeatFrame(HeldOrders):
    """One beat's forge and plan facts. Every standing rule reads them from here, so a rule that
    builds its own reader is a second copy of what GitHub said this beat."""

    pulls: ProgressRead
    plan: PlanReading
    answers: tuple[OwedAnswer, ...]
    closed_heads: tuple[StoredClosedHead, ...]
    head_reviews: Mapping[PullRequestHead, tuple[HeadReviewRecord, ...]]

    @classmethod
    def capture(cls, comms: Comms, commander: str, pulls: ProgressRead,
                plans: PlanReadings = PlanReadings()) -> Self:
        scan = HeldOrders.read(comms, commander)
        snapshot = comms.registry.snapshot()
        project = snapshot.commander_project.require()
        repos = project.repos
        repository = Repo.declaring(repos, PlanPhase.__module__)
        since = time.time() - ClosedUnmergedPullRequest.window
        with comms.goals.todos.snapshot() as db:
            closed = StoredClosedHead.current(db, project.repository_names(), since)
            reviews = StoredHeadReview.head_reviews_among(
                db, (head.request for head in closed))
        return cls(
            scan.live,
            scan.orphaned,
            pulls.register(scan.todos),
            plans.read(
                repository,
                comms.goals.todos.history_for_commander(snapshot.require(commander).incarnation),
                tuple(phase.phase_evidence() for phase in PlanPhase.members_with(PlanPhase)),
            ),
            OwedAnswer.read_scan(comms, commander, scan),
            closed,
            reviews,
        )
