"""The assignment read surface consumed by a goal, without a dependency on its stores."""

from abc import ABC, abstractmethod
from typing import Protocol, Self

from .nominal.field_codec import GoalId
from .thread_identity import ThreadIncarnation


class GoalAssignmentOrder[Assignment](Protocol):
    def instructions(self, assignment: Assignment) -> str: ...


class GoalAssignment(Protocol):
    @property
    def order(self) -> GoalAssignmentOrder[Self]: ...

    @property
    def pending(self) -> bool: ...

    def progress_line(self) -> str: ...


class GoalAssignments(Protocol):
    def for_goal(self, owner: ThreadIncarnation, goal_id: GoalId) -> tuple[GoalAssignment, ...]: ...

    def closed_for_goal(self, owner: ThreadIncarnation, goal_id: GoalId) -> tuple[GoalAssignment, ...]: ...

    def holds_pending(self, owner: ThreadIncarnation) -> bool: ...


class GoalWorkSource(ABC):
    @property
    @abstractmethod
    def todos(self) -> GoalAssignments: ...
