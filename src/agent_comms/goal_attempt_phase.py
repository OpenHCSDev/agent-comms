"""Attempt transitions, distinct from a goal generation and from launch authority."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState
from .lifecycle_capability import Failed, Terminal

if TYPE_CHECKING:
    import sqlite3
    from .goal_attempts import AttemptRecord


class ReadyAttempt(ABC):
    """Attempt outcomes that record their own evidence before a fresh grant."""

    @abstractmethod
    def record_ready(self, attempt: AttemptRecord, db: sqlite3.Connection, note: str) -> None: ...


@dataclass(frozen=True)
class GoalAttemptPhase(DeclaredFamily, LifecycleState, affix="Attempt"):
    launched: ClassVar[bool] = False
    verified_progress: ClassVar[bool] = False


class ReservedAttempt(GoalAttemptPhase):
    @classmethod
    def successors(cls):
        return ClaimedAttempt, FailedAttempt, ResolvedAttempt


class ClaimedAttempt(GoalAttemptPhase):
    launched = True

    @classmethod
    def successors(cls):
        return FailedAttempt, SucceededAttempt, ResolvedAttempt


class FailedAttempt(Failed, ReadyAttempt, GoalAttemptPhase):
    def record_ready(self, attempt: AttemptRecord, db: sqlite3.Connection, note: str) -> None:
        attempt.advance(db, self, resolution=note)

    @classmethod
    def successors(cls):
        return (ResolvedAttempt,)


class SucceededAttempt(Terminal, ReadyAttempt, GoalAttemptPhase):
    verified_progress = True

    def record_ready(self, attempt: AttemptRecord, db: sqlite3.Connection, note: str) -> None:
        attempt.advance(db, self, progress_witness=note)


class ResolvedAttempt(Terminal, GoalAttemptPhase):
    pass
