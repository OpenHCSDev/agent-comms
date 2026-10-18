"""Display behavior composed by goal declarations and unavailable observations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from agent_comms.goal_presentation import ThreadGoalExecution
    from agent_comms.goals import GoalDeclaration


class GoalDisplay(ABC):
    @property
    @abstractmethod
    def can_control(self) -> bool: ...

    @property
    @abstractmethod
    def snapshot(self) -> GoalDeclaration: ...

    @property
    @abstractmethod
    def visible(self) -> bool: ...

    @abstractmethod
    def heading(self, execution: ThreadGoalExecution) -> str: ...

    def details_heading(self, execution: ThreadGoalExecution) -> str:
        return self.heading(execution)

    @abstractmethod
    def standby(self, execution: ThreadGoalExecution) -> bool: ...

    @abstractmethod
    def action_failure(self, error: Exception) -> str: ...


@dataclass(frozen=True)
class GoalUnavailable(GoalDisplay):
    last_confirmed: GoalDeclaration

    can_control: ClassVar[bool] = False

    @property
    def snapshot(self) -> GoalDeclaration:
        return self.last_confirmed

    visible: ClassVar[bool] = True

    def standby(self, execution: ThreadGoalExecution) -> bool:
        return False

    def action_failure(self, error: Exception) -> str:
        return str(error)

    def heading(self, execution: ThreadGoalExecution) -> str:
        return self.last_confirmed.unavailable_heading
