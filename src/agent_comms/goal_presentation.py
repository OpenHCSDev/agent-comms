"""Behavior for the runnable/standby refinement of a goal's domain state."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .goal_states import GoalState

if TYPE_CHECKING:
    from .declarations import GoalExecution


class ExecutionPresentation(ABC):
    waiting = False

    @abstractmethod
    def render(self, execution: GoalExecution) -> tuple[str, str]: ...


@dataclass(frozen=True)
class StateExecutionPresentation(ExecutionPresentation):
    state: type[GoalState]

    def render(self, execution: GoalExecution) -> tuple[str, str]:
        return self.state.load(execution.block_reason, None).presentation()


class StandbyExecutionPresentation(ExecutionPresentation):
    waiting = True

    def render(self, execution: GoalExecution) -> tuple[str, str]:
        names = ", ".join(f"@{target.name}" for target in execution.wait_for)
        idle = ", ".join(f"@{target.name}" for target in execution.inactive_wait_for)
        suffix = f"; no active turn: {idle}" if idle else ""
        return "◌", f"Standby · waiting for {names}{suffix}"
