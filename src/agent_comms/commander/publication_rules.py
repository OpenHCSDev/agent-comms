"""Held work must be published, and a linked pull request must contain a change."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from agent_comms.field_codec import OrderId, PathText
from agent_comms.standing_rules import DatedStandingRule, HeadLine, HeldBranch, HeldBranchRule
from agent_comms.heartbeat_subjects import HeartbeatSubject, UnlandedHeadSubject
from agent_comms.head_review import PullRequestHead
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import PullRequestLink
from agent_comms.work_branches import UnpushedHead, WorkBranches

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from agent_comms.comms import Comms
    from agent_comms.heartbeat_frame import HeartbeatFrame
    from agent_comms.ui_model.unit_state import UnitState


@dataclass(frozen=True)
class UnpushedBranch(HeldBranchRule):
    problem: str
    order: OrderId
    holder: ThreadIncarnation
    worktree: Annotated[Path, PathText]
    # One decision per order branch: a new local commit moves the head, never the decision.
    unpublished: UnpushedHead = field(compare=False)

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        return tuple(cls(held.problem, held.todo.order.checked().id, held.holder,
                         held.worktree, unpublished)
                     for held in branches
                     for unpublished in held.repository.branches.unpushed(held.difference))

    def line(self) -> str:
        return (f"{self.holder.name} has {self.unpublished.commits} unpushed commits on"
                f" {self.problem} ({self.unpublished.head[:9]} in {self.worktree}, upstream"
                f" {self.unpublished.upstream or 'not configured'}). waiting for the disk to die?"
                " -> push them")


@dataclass(frozen=True)
class EmptyPullRequest(HeldBranchRule):
    problem: str
    order: OrderId
    holder: ThreadIncarnation
    pull_request: PullRequestLink
    base: str = field(compare=False)
    head: str

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        found = []
        for held in branches:
            reader = held.repository.branches
            todo = held.todo
            for pull in held.source.pulls.open_for(todo):
                for compared in pull.empty_changes(reader):
                    found.append(cls(held.problem, todo.order.checked().id, held.holder,
                                     PullRequestLink(todo.repo, pull.number),
                                     compared.base, compared.head))
        return tuple(found)

    def line(self) -> str:
        return (f"{self.pull_request.url} ({self.holder.name} {self.problem}) changes nothing"
                " against its base. a PR of nothing, why? -> restore the intended change or close"
                " the empty pull request")


@dataclass(frozen=True)
class ClosedUnmergedPullRequest(HeadLine, DatedStandingRule):
    """Closing a request does not land its patches or authorize throwing them away."""

    request: PullRequestHead
    closed_at: float
    commits: tuple[str, ...]
    window: ClassVar[float] = 7 * 24 * 60 * 60

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        found = []
        for repo, rows in frame.closed_pulls.items():
            remaining = tuple(pull for pull in rows if not any(review.verdict.supersedes()
                for review in frame.head_reviews[PullRequestHead(repo.github, pull.number, pull.head)]))
            patches = WorkBranches(repo.checkout).unlanded_among(tuple(pull.head for pull in remaining))
            found.extend(cls(request=PullRequestHead(repo.github, pull.number, pull.head),
                             closed_at=pull.closed_time, commits=missing)
                         for pull in remaining for missing in (patches[pull.head],) if missing)
        return tuple(found)

    def began(self) -> float:
        return self.closed_at

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        return (UnlandedHeadSubject(self.request),)

    def command_values(self) -> Mapping[str, str]:
        return {'pr': str(self.request.pr), 'head': self.request.head,
                'repo': self.request.repo.to_wire()}

    def line(self) -> str:
        pull = PullRequestLink(self.request.repo, self.request.pr)
        return (f'LOST {pull.url} closed unmerged holds {len(self.commits)} commits main lacks'
                ' -> land them, or record why they are dropped')
