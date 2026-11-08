"""Goal mutation contracts, independent of command and runtime infrastructure."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Protocol

from .attempt_resolution import WorkDecision
from .goal_attempt_identity import GoalAttemptIdentity
from .goal_turn_permit import GoalTurnPermit
from .nominal.field_codec import AttemptId, GoalId


class GoalMutation[Subject, Context, Result](ABC):
    @abstractmethod
    def change(self, goal: Subject, ctx: Context) -> Result: ...

    @abstractmethod
    def create(self, ctx: Context) -> Result: ...


class GoalStateOrder[Subject, Context](ABC):
    @abstractmethod
    def continue_goal(self, goal: Subject, ctx: Context) -> Subject: ...

    @abstractmethod
    def reactivate_goal(self, goal: Subject, ctx: Context) -> Subject: ...

    @abstractmethod
    def replace_goal(self, goal: Subject, ctx: Context) -> Subject: ...


class GoalStateProjection[Execution, Pause](ABC):
    @abstractmethod
    def active(self) -> Execution: ...

    @abstractmethod
    def paused(self, source: Pause) -> Execution: ...

    @abstractmethod
    def blocked(self, reason: str) -> Execution: ...

    @abstractmethod
    def completed(self) -> Execution: ...


class GoalFailureProjection[Execution, Failure](ABC):
    @abstractmethod
    def unresolved(self) -> Execution: ...

    @abstractmethod
    def failed(self, evidence: Failure) -> Execution: ...


class GoalGenerationProjection[Execution](ABC):
    @abstractmethod
    def turn_execution(self) -> Execution: ...

    @abstractmethod
    def unresolved(self) -> Execution: ...

    @abstractmethod
    def runnable(self) -> Execution: ...

    @abstractmethod
    def failed_execution(self) -> Execution: ...

    @abstractmethod
    def completed(self) -> Execution: ...


class GoalGenerationRead(Protocol):
    @property
    def goal_id(self) -> GoalId: ...

    @property
    def number(self) -> int: ...

    @property
    def attempt_identity(self) -> GoalAttemptIdentity: ...


class GoalGenerationChanges(ABC):
    @abstractmethod
    def authorize_ready_recovery(
        self, goal_id: GoalId, *, expected_generation: int, decision: WorkDecision,
    ) -> object: ...

    @abstractmethod
    def authorize_current_retry(self, goal_id: GoalId, decision: WorkDecision) -> None: ...

    @abstractmethod
    def authorize_retry(
        self, goal_id: GoalId, *, expected_generation: int, attempt_id: AttemptId,
        decision: WorkDecision,
    ) -> object: ...


class GoalStoppedAttempt[Reservation](Protocol):
    def stopped_permit(
        self, make_permit: Callable[[Reservation], GoalTurnPermit],
    ) -> GoalTurnPermit: ...


class GoalAttemptRead[Reservation](ABC):
    @abstractmethod
    def read_attempt(self, attempt_id: AttemptId) -> GoalStoppedAttempt[Reservation]: ...


class GoalGenerationGrants[Reservation](GoalAttemptRead[Reservation], ABC):
    @abstractmethod
    def reserve(
        self, goal_id: GoalId, expected_generation: int, *, ready_grant: str | None = None,
    ) -> Reservation: ...

    @abstractmethod
    def claim_launch(self, reservation: Reservation) -> GoalTurnPermit: ...


class GoalReadyGrantSource[Owner, Generation](ABC):
    @abstractmethod
    def ready_goal_grant_locked(
        self, owner: Owner, claim_scope: int, generation: Generation,
    ) -> str: ...


class GoalContextSource[Owner, Goals, Context](Protocol):
    def with_goal(self, owner: Owner, goals: Goals) -> Context: ...


class GoalFork[Owner, Declaration](ABC):
    @abstractmethod
    def fork_goal(self, parent: Owner, child: Owner) -> Declaration: ...
