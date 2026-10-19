"""The goal authority captured by an original or freshly accepted owner input."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .goals import Goal, GoalDeclaration


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
        return current.active().accepts_observation(self.goal.id, 0)


class InactiveGoalPermission(TurnGoalPermission):
    def allows(self, current: GoalDeclaration) -> bool:
        return not current.accepts_following_handoff()


@dataclass(frozen=True)
class AcceptedGoalPermission(TurnGoalPermission):
    goal_id: str | None

    def allows(self, current: GoalDeclaration) -> bool:
        active = current.active()
        return self.goal_id == active.reported_id()
