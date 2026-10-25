"""Which goals a read or check covers: every goal of the thread, or one goal by its ID."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from .field_codec import GoalId, NullMember, SingleFieldValue

if TYPE_CHECKING:
    from .goal_history import RecordedGoalRevision
    from .goals import GoalDeclaration


class FailureKey(TypedDict, total=False):
    """The attempt ledger columns a scope narrows a failure read by."""

    goal_id: GoalId


class GoalScope:
    """The default answers every goal of the thread."""

    __slots__ = ()

    def keeps(self, revision: RecordedGoalRevision) -> bool:
        return True

    def failure_key(self) -> FailureKey:
        return FailureKey()

    def require(self, goal: GoalDeclaration) -> None:
        """A check that names no goal holds for every goal."""

    def schedules_goal(self) -> bool:
        return False

    def retained_by(self, active_ids: Collection[GoalId]) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class AllGoals(GoalScope, NullMember):
    """No goal ID was given: every goal of the thread."""


@dataclass(frozen=True, slots=True)
class OneGoal(GoalScope, SingleFieldValue):
    """The revisions that name this goal before or after, and its failed attempts."""

    goal_id: GoalId

    def keeps(self, revision: RecordedGoalRevision) -> bool:
        return self.goal_id in {revision.before.reported_id(), revision.after.reported_id()}

    def failure_key(self) -> FailureKey:
        return FailureKey(goal_id=self.goal_id)

    def require(self, goal: GoalDeclaration) -> None:
        goal.require().require_id(self.goal_id)

    def schedules_goal(self) -> bool:
        return True

    def retained_by(self, active_ids: Collection[GoalId]) -> bool:
        return self.goal_id in active_ids
