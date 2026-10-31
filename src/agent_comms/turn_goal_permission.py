"""The goal authority captured by an original or freshly accepted owner input."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.nominal.field_codec import GoalId
from .goal_authority import GoalTurnAuthority

if TYPE_CHECKING:
    from .goal_waits import GoalWait
    from .goals import GoalDeclaration
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
    authority: GoalTurnAuthority

    def allows(self, current: GoalDeclaration) -> bool:
        return self.authority.keeps_turn_under(current.authority)


class PublishedGoalPermission(OwnerGoalPermission):
    """This running input itself wrote the captured declaration."""

    def allows_routed(self, current: GoalDeclaration) -> bool:
        return self.allows(current)

    def dependency_current(self, dependency: InputDependency, wait: GoalWait) -> bool:
        return dependency.current(wait) or wait.allows_routed_input()


@dataclass(frozen=True)
class ContinuationGoalPermission(GoalPermission):
    goal_id: GoalId

    def allows(self, current: GoalDeclaration) -> bool:
        return current.active().accepts_observation(self.goal_id, 0)


class InactiveGoalPermission(GoalPermission):
    def allows(self, current: GoalDeclaration) -> bool:
        return not current.accepts_following_handoff()


@dataclass(frozen=True)
class AcceptedGoalPermission(GoalPermission):
    goal_id: GoalId | None

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

    def published(self, goal: GoalDeclaration) -> None:
        self.captured = PublishedGoalPermission(goal.authority)
