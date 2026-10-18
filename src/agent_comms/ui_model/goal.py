"""UI presentations of a decoded goal declaration."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from agent_comms.declared_family import DeclaredFamily
from agent_comms.goals import NoGoal as NoGoalDeclaration

if TYPE_CHECKING:
    from agent_comms.goals import Goal, GoalDeclaration
    from agent_comms.goal_presentation import ThreadGoalExecution


class GoalDisplay(DeclaredFamily):
    can_control: ClassVar[bool] = True

    @classmethod
    def current(cls, goal):
        return goal.presented_by(cls)

    @classmethod
    def without_goal(cls):
        return NoGoal()

    @classmethod
    def with_goal(cls, goal):
        return ShowingGoal(goal)

    @property
    @abstractmethod
    def declaration(self) -> GoalDeclaration: ...

    @property
    def snapshot(self) -> Goal | None:
        return self.declaration.captured

    @property
    def visible(self) -> bool:
        return True

    def unavailable_heading(self) -> str:
        return "Goal state unavailable · last confirmed snapshot"

    @abstractmethod
    def heading(self, execution: ThreadGoalExecution) -> str: ...

    def details_heading(self, execution: ThreadGoalExecution) -> str:
        return self.heading(execution)

    def standby(self, execution: ThreadGoalExecution) -> bool:
        return False

    def action_failure(self, error: Exception) -> str:
        return str(error)


@dataclass(frozen=True)
class NoGoal(GoalDisplay):
    can_control: ClassVar[bool] = False

    @property
    def declaration(self):
        return NoGoalDeclaration()

    @property
    def visible(self) -> bool:
        return False

    def unavailable_heading(self) -> str:
        return "Goal state unavailable"

    def heading(self, execution: ThreadGoalExecution) -> str:
        return "No current goal"

    def action_failure(self, error: Exception) -> str:
        return "This goal has been cleared."


@dataclass(frozen=True)
class ShowingGoal(GoalDisplay):
    goal: Goal

    @property
    def declaration(self) -> Goal:
        return self.goal

    def standby(self, execution: ThreadGoalExecution) -> bool:
        return execution.waiting_for(self.goal)

    def heading(self, execution: ThreadGoalExecution) -> str:
        status = "Standby" if self.standby(execution) else self.goal.state.declared_name
        return f"Goal · {status} · rev {self.goal.revision}"

    def details_heading(self, execution: ThreadGoalExecution) -> str:
        heading = self.heading(execution)
        if self.standby(execution):
            heading += f" · {execution.presentation('').summary}"
        return heading


@dataclass(frozen=True)
class GoalUnavailable(GoalDisplay):
    last_confirmed: GoalDeclaration
    can_control: ClassVar[bool] = False

    @property
    def declaration(self) -> GoalDeclaration:
        return self.last_confirmed

    @property
    def visible(self) -> bool:
        return True

    def heading(self, execution: ThreadGoalExecution) -> str:
        return GoalDisplay.current(self.last_confirmed).unavailable_heading()
