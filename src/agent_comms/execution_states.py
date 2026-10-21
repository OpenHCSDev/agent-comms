"""Execution lifecycle: attempt ownership, transitions and coupled claim state."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .assignment_states import (
    AssignmentState,
    CompletedAssignment,
    DeferredAssignment,
    EngagedAssignment,
    FailedAssignment,
)
from .coordination_errors import IdentityConflict, IntegrityViolationError
from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState
from .lifecycle_capability import Active, Complete, Failed, Terminal
from .coordination_errors import ResponseClaimBlocked


if TYPE_CHECKING:
    from .coordination_snapshot import RecoverySnapshot


@dataclass(frozen=True)
class ExecutionState(DeclaredFamily, LifecycleState, affix="Execution"):
    starts_attempt: ClassVar[bool] = False
    retry: ClassVar[bool] = False
    queued: ClassVar[bool] = False
    unstarted: ClassVar[bool] = False

    @property
    def current_attempt_ordinal(self) -> int | None:
        return None

    @property
    def next_attempt_ordinal(self) -> int:
        return 1

    def require_attempt_start(self) -> None:
        raise IdentityConflict("execution cannot start an attempt")

    def require_retry_start(self, snapshot: RecoverySnapshot) -> None:
        pass

    @classmethod
    @abstractmethod
    def load(cls, ordinal: int | None) -> ExecutionState: ...

    @classmethod
    @abstractmethod
    def assignment_state(cls) -> type[AssignmentState]: ...

    def require_final_response(self) -> None:
        raise ResponseClaimBlocked()

    def validate_budget(self, maximum: int) -> None:
        ordinal = self.current_attempt_ordinal
        if ordinal is not None and not 1 <= ordinal <= maximum:
            raise ValueError("current ordinal exceeds budget")

    def has_retry_budget(self, maximum: int) -> bool:
        ordinal = self.current_attempt_ordinal
        return ordinal is not None and ordinal < maximum

    def can_retry(self, *, authorized: bool, attempt, is_current: bool) -> bool:
        return False

    def accepts_attempt(self, phase) -> bool:
        return False

    def accepts_previous(self, previous: ExecutionState) -> bool:
        return self.current_attempt_ordinal == previous.current_attempt_ordinal

    def validate_snapshot(self, snapshot, authorized: bool) -> None:
        """State-specific cross-lifecycle rules, after common identities agree."""


class UnstartedExecution(ExecutionState):
    unstarted = True

    @classmethod
    def load(cls, ordinal: int | None) -> ExecutionState:
        if ordinal is not None:
            raise IntegrityViolationError("unstarted execution cannot reference an attempt")
        return cls()

    @classmethod
    def assignment_state(cls) -> type[AssignmentState]:
        return EngagedAssignment


class QueuedExecution(UnstartedExecution):
    queued = True

    @classmethod
    def successors(cls):
        return PendingExecution, FailedExecution


class StartsAttempt:
    starts_attempt = True

    def require_attempt_start(self) -> None:
        pass


class PendingExecution(StartsAttempt, UnstartedExecution):

    @classmethod
    def successors(cls):
        return ActiveExecution, DeferredExecution, FailedExecution


class AttemptReference:
    """Shared ordinal projection for required and optional attempt references."""

    ordinal: int | None

    @property
    def current_attempt_ordinal(self) -> int | None:
        return self.ordinal

    @property
    def next_attempt_ordinal(self) -> int:
        return (self.ordinal or 0) + 1

    @classmethod
    def load(cls, ordinal: int | None) -> ExecutionState:
        return cls(ordinal)


@dataclass(frozen=True)
class AttemptExecution(AttemptReference, ExecutionState):
    ordinal: int

    def __post_init__(self):
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise IntegrityViolationError("active/completed execution requires an attempt")


class ActiveExecution(Active, AttemptExecution):

    def require_final_response(self) -> None:
        pass

    @classmethod
    def successors(cls):
        return DeferredExecution, CompletedExecution, FailedExecution

    @classmethod
    def assignment_state(cls) -> type[AssignmentState]:
        return EngagedAssignment

    def accepts_attempt(self, phase):
        return phase.accepts_active_execution()

    def accepts_previous(self, previous):
        return (
            previous.starts_attempt and self.ordinal == (previous.current_attempt_ordinal or 0) + 1
        )

    def validate_snapshot(self, snapshot, authorized):
        if not snapshot.is_current:
            raise IntegrityViolationError("active snapshot must carry exact owner pointer")


class CompletedExecution(Complete, Terminal, AttemptExecution):

    @classmethod
    def assignment_state(cls) -> type[AssignmentState]:
        return CompletedAssignment

    def accepts_attempt(self, phase):
        return phase.succeeded

    def validate_snapshot(self, snapshot, authorized):
        for row in snapshot.obligations:
            row.lifecycle.require_successful_completion(
                IntegrityViolationError("completed wire execution requires terminal obligation")
            )


@dataclass(frozen=True)
class InterruptedExecution(AttemptReference, ExecutionState):
    ordinal: int | None = None

    def accepts_attempt(self, phase):
        return phase.accepts_interrupted_execution()


class DeferredExecution(StartsAttempt, InterruptedExecution):
    retry = True

    def require_retry_start(self, snapshot: RecoverySnapshot) -> None:
        snapshot.previous_attempt.require_retry(snapshot)

    @classmethod
    def assignment_state(cls) -> type[AssignmentState]:
        return DeferredAssignment

    @classmethod
    def successors(cls):
        return ActiveExecution, FailedExecution

    def validate_budget(self, maximum: int) -> None:
        super().validate_budget(maximum)
        if self.ordinal is not None and self.ordinal >= maximum:
            raise IntegrityViolationError("post-attempt deferral requires retry budget")

    def validate_snapshot(self, snapshot, authorized):
        if snapshot.attempt is not None and not authorized:
            raise IntegrityViolationError("post-attempt deferral requires authorized retry")

    def can_retry(self, *, authorized: bool, attempt, is_current: bool) -> bool:
        if self.ordinal is None or attempt is None:
            return False
        # Outcome-owned permission is separate from final done/death evidence.
        return authorized and attempt.permits_retry() and not is_current


class FailedExecution(Failed, Terminal, InterruptedExecution):

    @classmethod
    def assignment_state(cls) -> type[AssignmentState]:
        return FailedAssignment

    def validate_snapshot(self, snapshot, authorized):
        if bool(snapshot.publication_evidence):
            raise IntegrityViolationError("failed execution cannot erase a publication acknowledgement")
        if snapshot.attempt is not None and authorized:
            raise IntegrityViolationError("authorized retry cannot settle failed")
