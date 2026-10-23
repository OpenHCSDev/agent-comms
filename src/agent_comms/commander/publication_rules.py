"""Held work must be published, and a linked pull request must contain a change."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from agent_comms.comms import Comms
from agent_comms.standing_rules import UndatedStandingRule
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.todos import PullRequestLink, UnmergedListedPullRequest
from agent_comms.ui_model.unit_state import UnitState
from agent_comms.work_branches import UnpushedHead, WorkBranches


@dataclass(frozen=True)
class UnpushedBranch(UndatedStandingRule):
    problem: str
    order: str
    holder: ThreadIncarnation
    worktree: Path
    unpublished: UnpushedHead

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState]) -> tuple[Self, ...]:
        return tuple(cls(todo.id, todo.order.checked().id, unit,
                         state.source.thread.project_path, unpublished)
                     for state in observed for unit in state.readable_units()
                     if state.source.todos
                     for unpublished in WorkBranches(state.source.thread.project_path).unpushed()
                     for todo in state.source.todos)

    def line(self) -> str:
        return (f"UNPUSHED: {self.order} ({self.problem}), holder {self.holder.name}, "
                f"{self.worktree}: {self.unpublished.commits} unpushed commits at "
                f"{self.unpublished.head}; upstream {self.unpublished.upstream or 'not configured'}"
                " -> commit and push the held work")


@dataclass(frozen=True)
class EmptyPullRequest(UndatedStandingRule):
    problem: str
    order: str
    holder: ThreadIncarnation
    pull_request: PullRequestLink
    base: str = field(compare=False)
    head: str

    @classmethod
    def scan_observed(cls, comms: Comms, commander: str,
                      observed: Sequence[UnitState]) -> tuple[Self, ...]:
        found = []
        for state in observed:
            for unit in state.readable_units():
                branches = WorkBranches(state.source.thread.project_path)
                for todo in state.source.todos:
                    for pull in state.source.pulls.listed(todo.repo, UnmergedListedPullRequest, todo):
                        compared = branches.pull_request_diff(pull.base, pull.head)
                        if not compared.paths:
                            found.append(cls(todo.id, todo.order.checked().id, unit,
                                             PullRequestLink(todo.repo, pull.number),
                                             compared.base, compared.head))
        return tuple(found)

    def line(self) -> str:
        return (f"EMPTY PR: {self.order} ({self.problem}), holder {self.holder.name}, "
                f"{self.pull_request.url} has no change against its base"
                " -> restore the intended change or close the empty pull request")
