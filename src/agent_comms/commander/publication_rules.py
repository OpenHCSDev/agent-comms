"""Held work must be published, and a linked pull request must contain a change."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from agent_comms.comms import Comms
from agent_comms.standing_rules import UndatedStandingRule
from agent_comms.thread_identity import ThreadIncarnation
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
                     for unpublished in WorkBranches(state.source.thread.project_path).unpushed()
                     for todo in state.source.todos)

    def line(self) -> str:
        return (f"UNPUSHED: {self.order} ({self.problem}), holder {self.holder.name}, "
                f"{self.worktree}: {self.unpublished.commits} unpushed commits at "
                f"{self.unpublished.head}; upstream {self.unpublished.upstream or 'not configured'}"
                " -> commit and push the held work")
