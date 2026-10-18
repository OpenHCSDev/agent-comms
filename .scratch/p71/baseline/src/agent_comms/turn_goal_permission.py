"""The goal authority captured by an original or freshly accepted owner input."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .goals import Goal


class TurnGoalPermission(ABC):
    @abstractmethod
    def allows(self, current: Goal | None) -> bool: ...


@dataclass(frozen=True)
class OwnerGoalPermission(TurnGoalPermission):
    goal: Goal | None

    def allows(self, current: Goal | None) -> bool:
        return current == self.goal


@dataclass(frozen=True)
class ContinuationGoalPermission(TurnGoalPermission):
    goal: Goal

    def allows(self, current: Goal | None) -> bool:
        return current is not None and current.id == self.goal.id and current.state.active


class InactiveGoalPermission(TurnGoalPermission):
    def allows(self, current: Goal | None) -> bool:
        return current is None or not current.state.active


@dataclass(frozen=True)
class AcceptedGoalPermission(TurnGoalPermission):
    goal_id: str | None

    def allows(self, current: Goal | None) -> bool:
        return self.goal_id == (
            current.id if current is not None and current.state.active else None
        )
