"""The goal permission captured by an original or freshly accepted owner input."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .goals import Goal, GoalDeclaration, NoGoal


class TurnGoalPermission(ABC):
    _serves: ClassVar[dict[type[GoalDeclaration], type[TurnGoalPermission]]] = {}

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        served = cls.__dict__.get("serves")
        if served is not None:
            if served in TurnGoalPermission._serves:
                raise TypeError(f"Duplicate continuation permission for {served.__name__}")
            TurnGoalPermission._serves[served] = cls

    @abstractmethod
    def allows(self, current: GoalDeclaration) -> bool: ...

    @classmethod
    def for_continuation(cls, goal: GoalDeclaration) -> TurnGoalPermission:
        return cls._serves[type(goal)].of(goal)


@dataclass(frozen=True)
class OwnerGoalPermission(TurnGoalPermission):
    goal: GoalDeclaration

    def allows(self, current: GoalDeclaration) -> bool:
        return current == self.goal


@dataclass(frozen=True)
class ContinuationGoalPermission(TurnGoalPermission):
    goal: Goal
    serves = Goal

    @classmethod
    def of(cls, goal: Goal) -> ContinuationGoalPermission:
        return cls(goal)

    def allows(self, current: GoalDeclaration) -> bool:
        return current.permits_continuation(self.goal.id)


class InactiveGoalPermission(TurnGoalPermission):
    serves = NoGoal

    @classmethod
    def of(cls, goal: NoGoal) -> InactiveGoalPermission:
        return cls()

    def allows(self, current: GoalDeclaration) -> bool:
        return current.active().permits_inactive_input()


@dataclass(frozen=True)
class AcceptedGoalPermission(TurnGoalPermission):
    goal_id: str | None

    def allows(self, current: GoalDeclaration) -> bool:
        return current.permits_accepted(self.goal_id)
