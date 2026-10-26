"""What one heartbeat reads from GitHub and the plans, read once and shared by every rule."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import time

from agent_comms.commander.publication_rules import ClosedUnmergedPullRequest
from agent_comms.head_review import PullRequestHead
from typing import TYPE_CHECKING, Self

from agent_comms.held_orders import HeldOrders
from agent_comms.phase_evidence import PlanReading, PlanReadings
from agent_comms.plan_phases import PlanPhase
from agent_comms.repo import Repo
from agent_comms.standing_rules import OwedAnswer

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.todos import ClosedListedPullRequest, HeadReviewRecord, ProgressRead


@dataclass(frozen=True)
class HeartbeatFrame(HeldOrders):
    """One beat's forge and plan facts. Every standing rule reads them from here, so a rule that
    builds its own reader is a second copy of what GitHub said this beat."""

    pulls: ProgressRead
    plan: PlanReading
    answers: tuple[OwedAnswer, ...]
    closed_pulls: Mapping[Repo, tuple[ClosedListedPullRequest, ...]]
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
        closed = {Repo.selected(repos, name.to_wire()): pulls.closed_unmerged(name, since)
                  for name in project.repository_names()}
        reviews = comms.goals.todos.head_reviews_among(
            PullRequestHead(repo.github, pull.number, pull.head)
            for repo, rows in closed.items() for pull in rows)
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
