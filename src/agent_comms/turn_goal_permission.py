"""The goal authority captured by an original or freshly accepted owner input."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .goals import Goal, ThreadGoal


class TurnGoalPermission(ABC):
    @abstractmethod
    def allows(self, current: ThreadGoal) -> bool: ...


@dataclass(frozen=True)
class OwnerGoalPermission(TurnGoalPermission):
    goal: ThreadGoal

    def allows(self, current: ThreadGoal) -> bool:
        return current == self.goal


@dataclass(frozen=True)
class ContinuationGoalPermission(TurnGoalPermission):
    goal: Goal

    def allows(self, current: ThreadGoal) -> bool:
        return current.active.identifies(self.goal.id)


class InactiveGoalPermission(TurnGoalPermission):
    def allows(self, current: ThreadGoal) -> bool:
        return current.active.fold(lambda goal: False, lambda: True)


@dataclass(frozen=True)
class AcceptedGoalPermission(TurnGoalPermission):
    goal_id: str | None

    def allows(self, current: ThreadGoal) -> bool:
        return self.goal_id == current.active.fold(lambda goal: goal.id, lambda: None)
