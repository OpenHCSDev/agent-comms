from __future__ import annotations

from abc import (
    ABC,
    abstractmethod,
)
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.nominal.field_codec import GoalId
from agent_comms.planning.goal_authority import GoalTurnAuthority

if TYPE_CHECKING:
    from agent_comms.execution.turn_input_source import InputDependency
    from agent_comms.planning.goal_waits import GoalWait
    from agent_comms.planning.goals import GoalDeclaration


class GoalPermission(ABC):
    @abstractmethod
    def allows(self, current: GoalDeclaration) -> bool: ...

    def describe(self) -> str:
        """How a refusal names this captured permission; a member with an authority names it."""
        return "this input's captured permission"

    def allows_routed(self, current: GoalDeclaration) -> bool:
        return not current.accepts_following_handoff()

    def dependency_current(self, dependency: InputDependency, wait: GoalWait) -> bool:
        return dependency.current(wait)


@dataclass(frozen=True)
class OwnerGoalPermission(GoalPermission):
    authority: GoalTurnAuthority

    def allows(self, current: GoalDeclaration) -> bool:
        return self.authority.keeps_turn_under(current.authority)

    def describe(self) -> str:
        return self.authority.describe()


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

    def describe(self) -> str:
        return f"continuation of goal {self.goal_id}"


class InactiveGoalPermission(GoalPermission):
    def allows(self, current: GoalDeclaration) -> bool:
        return not current.accepts_following_handoff()


@dataclass(frozen=True)
class AcceptedGoalPermission(GoalPermission):
    goal_id: GoalId | None

    def allows(self, current: GoalDeclaration) -> bool:
        active = current.active()
        return self.goal_id == active.reported_id()

    def describe(self) -> str:
        return f"an accepted owner input for goal {self.goal_id}"


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

    def describe(self) -> str:
        return self.captured.describe()

    def published(self, goal: GoalDeclaration) -> None:
        self.captured = PublishedGoalPermission(goal.authority)
