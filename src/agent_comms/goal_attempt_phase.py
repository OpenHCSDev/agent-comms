"""Attempt transitions, distinct from a goal generation and from launch authority."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .goal_attempt_identity import FailureNotObserved
from .lifecycle import LifecycleState

if TYPE_CHECKING:
    import sqlite3
    from .goal_attempts import AttemptRecord


class ReadyAttempt(ABC):
    """Attempt outcomes that record their own evidence before a fresh grant."""

    @abstractmethod
    def record_ready(self, attempt: AttemptRecord, db: sqlite3.Connection, note: str) -> None: ...


@dataclass(frozen=True)
class GoalAttemptPhase(DeclaredFamily, LifecycleState, affix="Attempt"):
    terminal: ClassVar[bool] = False
    launched: ClassVar[bool] = False
    verified_progress: ClassVar[bool] = False

    def require_failure(self) -> None:
        raise FailureNotObserved("missing_binding")

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[GoalAttemptPhase], ...]: ...


class ReservedAttempt(GoalAttemptPhase):
    @classmethod
    def successors(cls):
        return ClaimedAttempt, FailedAttempt, ResolvedAttempt


class ClaimedAttempt(GoalAttemptPhase):
    launched = True

    @classmethod
    def successors(cls):
        return FailedAttempt, SucceededAttempt, ResolvedAttempt


class FailedAttempt(ReadyAttempt, GoalAttemptPhase):
    def record_ready(self, attempt: AttemptRecord, db: sqlite3.Connection, note: str) -> None:
        attempt.advance(db, self, resolution=note)

    def require_failure(self) -> None:
        return None

    @classmethod
    def successors(cls):
        return (ResolvedAttempt,)


class SucceededAttempt(ReadyAttempt, GoalAttemptPhase):
    terminal = verified_progress = True

    def record_ready(self, attempt: AttemptRecord, db: sqlite3.Connection, note: str) -> None:
        attempt.advance(db, self, progress_witness=note)

    @classmethod
    def successors(cls):
        return ()


class ResolvedAttempt(GoalAttemptPhase):
    terminal = True

    @classmethod
    def successors(cls):
        return ()
