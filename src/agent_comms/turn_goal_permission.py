"""The goal authority captured by an original or freshly accepted owner input."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .goals import Goal, GoalDeclaration


class GoalPermission(ABC):
    @abstractmethod
    def allows(self, current: GoalDeclaration) -> bool: ...

    def allows_routed(self, current: GoalDeclaration) -> bool:
        return not current.accepts_following_handoff()

    def dependency_current(self, dependency, wait) -> bool:
        return dependency.current(wait)


@dataclass(frozen=True)
class OwnerGoalPermission(GoalPermission):
    goal: GoalDeclaration

    def allows(self, current: GoalDeclaration) -> bool:
        return current == self.goal


class PublishedGoalPermission(OwnerGoalPermission):
    """This running input itself wrote the captured declaration."""

    def allows_routed(self, current: GoalDeclaration) -> bool:
        return self.allows(current)

    def dependency_current(self, dependency, wait) -> bool:
        return dependency.current(wait) or wait.allows_routed_input()


@dataclass(frozen=True)
class ContinuationGoalPermission(GoalPermission):
    goal: Goal

    def allows(self, current: GoalDeclaration) -> bool:
        return current.active().accepts_observation(self.goal.id, 0)


class InactiveGoalPermission(GoalPermission):
    def allows(self, current: GoalDeclaration) -> bool:
        return not current.accepts_following_handoff()


@dataclass(frozen=True)
class AcceptedGoalPermission(GoalPermission):
    goal_id: str | None

    def allows(self, current: GoalDeclaration) -> bool:
        active = current.active()
        return self.goal_id == active.reported_id()


@dataclass
class TurnGoalPermission:
    """One live input's grant, advanced only by its authenticated tool publication.

    The returned declaration is the writer's receipt, not a reread of whatever
    another owner may have published. A later external change still fails equality.
    """

    captured: GoalPermission

    def allows(self, current: GoalDeclaration) -> bool:
        return self.captured.allows(current)

    def allows_routed(self, current: GoalDeclaration) -> bool:
        return self.captured.allows_routed(current)

    def dependency_current(self, dependency, wait) -> bool:
        return self.captured.dependency_current(dependency, wait)

    def published(self, goal: GoalDeclaration) -> None:
        self.captured = PublishedGoalPermission(goal)
