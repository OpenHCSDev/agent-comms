"""Held work must be published, and a linked pull request must contain a change."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Self

from agent_comms.standing_rules import HeldBranch, HeldBranchRule
from agent_comms.field_codec import PathText
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import PullRequestLink, UnmergedListedPullRequest
from agent_comms.work_branches import UnpushedHead


@dataclass(frozen=True)
class UnpushedBranch(HeldBranchRule):
    problem: str
    order: str
    holder: ThreadIncarnation
    worktree: Annotated[Path, PathText]
    unpublished: UnpushedHead

    @classmethod
    def derive(cls, branches: tuple[HeldBranch, ...]) -> tuple[Self, ...]:
        return tuple(cls(held.problem, held.todo.order.checked().id, held.holder,
                         held.source.thread.project_path, unpublished)
                     for held in branches
                     for unpublished in held.source.pulls.checkout(
                         held.source.thread.project_path).branches.unpushed(held.difference))

    def line(self) -> str:
        return (f"UNPUSHED: {self.order} ({self.problem}), holder {self.holder.name}, "
                f"{self.worktree}: unpushed commits: {self.unpublished.commits}, HEAD "
                f"{self.unpublished.head}; upstream {self.unpublished.upstream or 'not configured'}"
                " -> publish this HEAD and set its upstream")


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
            reader = held.source.pulls.checkout(held.source.thread.project_path).branches
            todo = held.todo
            for pull in held.source.pulls.listed(todo.repo, UnmergedListedPullRequest, todo):
                compared = reader.pull_request_diff(pull.base, pull.head)
                if not compared.paths:
                    found.append(cls(held.problem, todo.order.checked().id, held.holder,
                                     PullRequestLink(todo.repo, pull.number),
                                     compared.base, compared.head))
        return tuple(found)

    def line(self) -> str:
        return (f"EMPTY PR: {self.order} ({self.problem}), holder {self.holder.name}, "
                f"{self.pull_request.url} has no change against its base"
                " -> restore the intended change or close the empty pull request")
