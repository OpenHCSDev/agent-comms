"""Execution lifecycle: attempt ownership, transitions and coupled claim state."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .coordination_errors import IntegrityViolationError
from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState


@dataclass(frozen=True)
class ExecutionState(DeclaredFamily, LifecycleState, affix="Execution"):
    active: ClassVar[bool] = False
    terminal: ClassVar[bool] = False
    starts_attempt: ClassVar[bool] = False
    retry: ClassVar[bool] = False
    queued: ClassVar[bool] = False
    unstarted: ClassVar[bool] = False
    completed: ClassVar[bool] = False
    failed: ClassVar[bool] = False

    @property
    def current_attempt_ordinal(self) -> int | None:
        return None

    @classmethod
    @abstractmethod
    def load(cls, ordinal: int | None) -> ExecutionState: ...

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[ExecutionState], ...]: ...

    @property
    def claim_disposition(self) -> str:
        return self.declared_name

    def validate_budget(self, maximum: int) -> None:
        ordinal = self.current_attempt_ordinal
        if ordinal is not None and not 1 <= ordinal <= maximum:
            raise ValueError("current ordinal exceeds budget")

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

    @property
    def claim_disposition(self) -> str:
        return "engaged"


class QueuedExecution(UnstartedExecution):
    queued = True

    @classmethod
    def successors(cls):
        return PendingExecution, FailedExecution


class PendingExecution(UnstartedExecution):
    starts_attempt = True

    @classmethod
    def successors(cls):
        return ActiveExecution, DeferredExecution, FailedExecution


class AttemptReference:
    """Shared ordinal projection for required and optional attempt references."""

    ordinal: int | None

    @property
    def current_attempt_ordinal(self) -> int | None:
        return self.ordinal

    @classmethod
    def load(cls, ordinal: int | None) -> ExecutionState:
        return cls(ordinal)


@dataclass(frozen=True)
class AttemptExecution(AttemptReference, ExecutionState):
    ordinal: int

    def __post_init__(self):
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise IntegrityViolationError("active/completed execution requires an attempt")


class ActiveExecution(AttemptExecution):
    active = True

    @classmethod
    def successors(cls):
        return DeferredExecution, CompletedExecution, FailedExecution

    @property
    def claim_disposition(self) -> str:
        return "engaged"

    def accepts_attempt(self, phase):
        return not phase.terminal

    def accepts_previous(self, previous):
        return (
            previous.starts_attempt and self.ordinal == (previous.current_attempt_ordinal or 0) + 1
        )

    def validate_snapshot(self, snapshot, authorized):
        if not snapshot.is_current:
            raise IntegrityViolationError("active snapshot must carry exact owner pointer")


class CompletedExecution(AttemptExecution):
    terminal = True
    completed = True

    @classmethod
    def successors(cls):
        return ()

    def accepts_attempt(self, phase):
        return phase.succeeded

    def validate_snapshot(self, snapshot, authorized):
        if snapshot.execution.exact_target is not None and (
            snapshot.obligation is None or not snapshot.obligation.lifecycle.successful
        ):
            raise IntegrityViolationError("completed wire execution requires terminal obligation")


@dataclass(frozen=True)
class InterruptedExecution(AttemptReference, ExecutionState):
    ordinal: int | None = None

    def accepts_attempt(self, phase):
        return phase.failed


class DeferredExecution(InterruptedExecution):
    starts_attempt = True
    retry = True

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


class FailedExecution(InterruptedExecution):
    terminal = True
    failed = True

    @classmethod
    def successors(cls):
        return ()

    def validate_snapshot(self, snapshot, authorized):
        if snapshot.publication_receipt is not None:
            raise IntegrityViolationError("failed execution cannot erase a publication receipt")
        if snapshot.attempt is not None and authorized:
            raise IntegrityViolationError("authorized retry cannot settle failed")
