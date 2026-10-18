"""The goal authority captured by an original or freshly accepted owner input."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .goals import GoalDeclaration
from .goals import Goal


class TurnGoalPermission(ABC):
    @abstractmethod
    def allows(self, current: GoalDeclaration) -> bool: ...


@dataclass(frozen=True)
class OwnerGoalPermission(TurnGoalPermission):
    goal: GoalDeclaration

    def allows(self, current: GoalDeclaration) -> bool:
        return current == self.goal


@dataclass(frozen=True)
class ContinuationGoalPermission(TurnGoalPermission):
    goal: Goal

    def allows(self, current: GoalDeclaration) -> bool:
        return current.permits_continuation(self.goal.id)


class InactiveGoalPermission(TurnGoalPermission):
    def allows(self, current: GoalDeclaration) -> bool:
        return current.active().permits_inactive_input()


@dataclass(frozen=True)
class AcceptedGoalPermission(TurnGoalPermission):
    goal_id: str | None

    def allows(self, current: GoalDeclaration) -> bool:
        return current.permits_accepted(self.goal_id)
