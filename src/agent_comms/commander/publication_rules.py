"""Held work must be published, and a linked pull request must contain a change."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Self

from agent_comms.field_codec import PathText
from agent_comms.standing_rules import HeldBranch, HeldBranchRule
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import PullRequestLink, UnmergedListedPullRequest
from agent_comms.work_branches import UnpushedHead


@dataclass(frozen=True)
class UnpushedBranch(HeldBranchRule):
    problem: str
    order: str
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
        return (f"PUSH {self.holder.name} {self.problem} {self.unpublished.head[:9]}: "
                f"{self.unpublished.commits} unpushed commits in {self.worktree}, upstream "
                f"{self.unpublished.upstream or 'not configured'}")


@dataclass(frozen=True)
class EmptyPullRequest(HeldBranchRule):
    problem: str
    order: str
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
            for pull in held.source.pulls.listed(todo.repo, UnmergedListedPullRequest, todo):
                for compared in pull.empty_changes(reader):
                    found.append(cls(held.problem, todo.order.checked().id, held.holder,
                                     PullRequestLink(todo.repo, pull.number),
                                     compared.base, compared.head))
        return tuple(found)

    def line(self) -> str:
        return (f"FILL {self.pull_request.url} ({self.holder.name} {self.problem}): no change"
                " against its base -> restore the intended change or close the empty pull request")
