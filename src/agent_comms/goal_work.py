"""The assignment read surface consumed by a goal, without a dependency on its stores."""

from abc import ABC, abstractmethod
from typing import Protocol, Self

from agent_comms.execution.child_process import ProcessState
from agent_comms.nominal.field_codec import GoalId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import ObservedTurn
from agent_comms.registry.thread_identity import ThreadIncarnation


class GoalObservation(Protocol):
    @property
    def captured(self) -> object: ...

    def accepts_observation(self, goal_id: GoalId, minimum_revision: int) -> bool: ...


class GoalOwner(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def incarnation(self) -> ThreadIncarnation: ...

    @property
    def goal(self) -> GoalObservation: ...

    def require_goal_assignment(self, actor: ThreadIncarnation) -> None: ...


class GoalFailureOwner(GoalOwner, Protocol):
    @property
    def worktree(self) -> Checkout: ...

    @property
    def process_identity(self) -> ProcessState: ...

    def observed_turn(self, claim_scope: int) -> ObservedTurn: ...


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
