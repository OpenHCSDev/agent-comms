"""The goal authority captured by an original or freshly accepted owner input."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .native_tool_call import SelectedToolDenied
from .ordinary_turn_rules import OrdinaryGoalRule
from .reservation_rules import ReservationViolationError

if TYPE_CHECKING:
    from .goal_waits import GoalWait
    from .goals import Goal, GoalDeclaration
    from .turn_input_source import InputDependency


class GoalPermission(ABC):
    @abstractmethod
    def allows(self, current: GoalDeclaration) -> bool: ...

    def allows_routed(self, current: GoalDeclaration) -> bool:
        return not current.accepts_following_handoff()

    def dependency_current(self, dependency: InputDependency, wait: GoalWait) -> bool:
        return dependency.current(wait)


@dataclass(frozen=True)
class OwnerGoalPermission(GoalPermission):
    goal: GoalDeclaration

    def allows(self, current: GoalDeclaration) -> bool:
        return self.goal.authority.keeps_turn_under(current.authority)


class PublishedGoalPermission(OwnerGoalPermission):
    """This running input itself wrote the captured declaration."""

    def allows_routed(self, current: GoalDeclaration) -> bool:
        return self.allows(current)

    def dependency_current(self, dependency: InputDependency, wait: GoalWait) -> bool:
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
class TurnGoalPermission(GoalPermission):
    """One running input's permission, changed only by its own tool publication.

    The writer returns its exact declaration, not a reread of whatever another
    owner may have published. A later external change fails unless the current goal state
    continues the captured one (GoalState.continues_turn_from).
    """

    captured: GoalPermission

    def allows(self, current: GoalDeclaration) -> bool:
        return self.captured.allows(current)

    def allows_routed(self, current: GoalDeclaration) -> bool:
        return self.captured.allows_routed(current)

    def dependency_current(self, dependency: InputDependency, wait: GoalWait) -> bool:
        return self.captured.dependency_current(dependency, wait)

    def require_current(self, current: GoalDeclaration) -> None:
        if not self.allows(current):
            raise SelectedToolDenied(str(ReservationViolationError(OrdinaryGoalRule())))

    def published(self, goal: GoalDeclaration) -> None:
        self.captured = PublishedGoalPermission(goal)
