"""Held work must be published, and a linked pull request must contain a change."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self

from agent_comms.heartbeat_subjects import HeartbeatSubject, UnlandedHeadSubject
from agent_comms.nominal.field_codec import PathText
from agent_comms.planning.pull_requests import PullRequestLink
from agent_comms.projects.checkouts import UnpushedHead
from agent_comms.projects.head_review import PullRequestHead
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.work_branches import CommitBase, WorkBranches
from agent_comms.team.standing_rules import (
    DatedStandingRule,
    HeadLine,
    HeldBranch,
    HeldOrderBranchRule,
    UndatedStandingRule,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agent_comms.comms import Comms
    from agent_comms.team.standing_rules import HeartbeatFrame
    from agent_comms.ui_model.teammate_state import TeammateState


@dataclass(frozen=True)
class UnpushedBranch(HeldOrderBranchRule):
    worktree: Annotated[Path, PathText]
    # One decision per order branch: a new local commit moves the head, never the decision.
    unpublished: UnpushedHead = field(compare=False)

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        return tuple(cls(holder=held.holder, problem=held.problem,
                         order=held.todo.order.checked().id, worktree=held.worktree,
                         unpublished=unpublished)
                     for held in branches
                     for unpublished in held.difference.unpublished)

    def line(self) -> str:
        return (f"{self.holder.name} has {self.unpublished.commits} unpushed commits on"
                f" {self.problem} ({self.unpublished.head[:9]} in {self.worktree}, upstream"
                f" {self.unpublished.upstream or 'not configured'}). these commits exist only locally"
                " -> push them")


@dataclass(frozen=True)
class EmptyPullRequest(HeldOrderBranchRule):
    pull_request: PullRequestLink
    base: str = field(compare=False)
    head: str

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        found = []
        for held in branches:
            reader = held.repository.branches
            todo = held.todo
            for pull in held.source.frame.pulls.open_for(todo):
                for compared in pull.empty_changes(reader):
                    found.append(cls(holder=held.holder, problem=held.problem,
                                     order=todo.order.checked().id,
                                     pull_request=PullRequestLink(todo.repo, pull.number),
                                     base=compared.base, head=compared.head))
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
    base: CommitBase

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[TeammateState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return tuple(cls(request=head.request, closed_at=head.closed_at, commits=head.missing,
                         base=head.base)
                     for head in frame.closed.lost if head.missing
                     if not any(review.verdict.supersedes()
                                for review in frame.head_reviews[head.request]))

    def began(self) -> float:
        return self.closed_at

    def subjects(self) -> tuple[HeartbeatSubject, ...]:
        return (UnlandedHeadSubject(self.request),)

    def command_values(self) -> Mapping[str, str]:
        return {'pr': str(self.request.pr), 'head': self.request.head,
                'repo': self.request.repo.to_wire()}

    def line(self) -> str:
        pull = PullRequestLink(self.request.repo, self.request.pr)
        return (f'LOST {pull.url} closed unmerged {self.base.description(len(self.commits))}'
                ' -> land them, or record why they are dropped')


@dataclass(frozen=True)
class ClosedPullRequestScanPending(UndatedStandingRule):
    """A repository without a completed closed-PR scan cannot say whether it lost commits."""

    repo: RepositoryName

    @classmethod
    def scan_observed(cls, comms: Comms,
                      observed: Sequence[TeammateState], frame: HeartbeatFrame) -> tuple[Self, ...]:
        return tuple(cls(repo) for repo in frame.closed.unscanned)

    def line(self) -> str:
        return (f'closed-PR scan for {self.repo.to_wire()} has not completed yet;'
                ' lost closed heads there are unknown')
