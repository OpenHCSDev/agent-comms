"""Display a current goal declaration or the failure to read one."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field

from agent_comms import goals
from agent_comms.declared_family import DeclaredFamily
from agent_comms.goal_presentation import ThreadGoalExecution
from agent_comms.goals import GoalDeclaration
from agent_comms.thread_presentation import ThreadPresentation


class GoalDisplay(DeclaredFamily):
    @classmethod
    def current(cls, goal: GoalDeclaration) -> CurrentGoal:
        return CurrentGoal(goal)

    @property
    def can_control(self) -> bool:
        return True

    @property
    @abstractmethod
    def snapshot(self) -> GoalDeclaration: ...

    @property
    def visible(self) -> bool:
        return True

    @abstractmethod
    def heading(self, execution: ThreadGoalExecution) -> str: ...

    def details_heading(self, execution: ThreadGoalExecution) -> str:
        return self.heading(execution)

    def standby(self, execution: ThreadGoalExecution) -> bool:
        return False

    def action_failure(self, error: Exception) -> str:
        return str(error)


@dataclass(frozen=True)
class CurrentGoal(GoalDisplay):
    goal: GoalDeclaration

    @property
    def snapshot(self) -> GoalDeclaration:
        return self.goal

    @property
    def can_control(self) -> bool:
        return self.goal.control_available()

    @property
    def visible(self) -> bool:
        return self.goal.panel_visible()

    def standby(self, execution: ThreadGoalExecution) -> bool:
        return self.goal.display_standby(execution)

    def heading(self, execution: ThreadGoalExecution) -> str:
        return self.goal.display_heading(execution)

    def details_heading(self, execution: ThreadGoalExecution) -> str:
        heading = self.heading(execution)
        if self.standby(execution):
            heading += f" · {execution.waiting_presentation(ThreadPresentation('', '', '')).summary}"
        return heading

    def action_failure(self, error: Exception) -> str:
        return self.goal.control_failure(error)


@dataclass(frozen=True)
class GoalUnavailable(GoalDisplay):
    last_confirmed: GoalDeclaration = field(default_factory=goals.NoGoal)

    @property
    def can_control(self) -> bool:
        return False

    @property
    def snapshot(self) -> GoalDeclaration:
        return self.last_confirmed

    def heading(self, execution: ThreadGoalExecution) -> str:
        return self.last_confirmed.unavailable_heading()
