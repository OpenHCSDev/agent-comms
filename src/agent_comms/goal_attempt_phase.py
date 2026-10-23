"""Attempt transitions, distinct from a goal generation and from launch authority."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .errors import UnresolvedAttemptError
from .lifecycle import LifecycleState
from .lifecycle_capability import Failed, Terminal

if TYPE_CHECKING:
    import sqlite3
    from .goal_attempts import AttemptRecord


class FromReservedAttempt:
    @classmethod
    def after_reserved_attempt(cls) -> bool:
        return True


class FromClaimedAttempt:
    @classmethod
    def after_claimed_attempt(cls) -> bool:
        return True


class FromFailedAttempt:
    @classmethod
    def after_failed_attempt(cls) -> bool:
        return True



class ReadyAttempt(ABC):
    """Attempt outcomes that record their own evidence before a fresh grant."""

    @abstractmethod
    def record_ready(self, attempt: AttemptRecord, db: sqlite3.Connection, note: str) -> None: ...


@dataclass(frozen=True)
class GoalAttemptPhase(DeclaredFamily, LifecycleState, affix="Attempt"):
    launched: ClassVar[bool] = False
    verified_progress: ClassVar[bool] = False
    permits_retirement: ClassVar[bool] = False

    def require_retryable(self) -> None:
        raise UnresolvedAttemptError("Only a blocked, recorded failure may be retried explicitly.")


class ReservedAttempt(GoalAttemptPhase):
    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromReservedAttempt.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_reserved_attempt()


class ClaimedAttempt(FromReservedAttempt, GoalAttemptPhase):
    launched = True

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromClaimedAttempt.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_claimed_attempt()


class FailedAttempt(FromReservedAttempt, FromClaimedAttempt, Failed, ReadyAttempt, GoalAttemptPhase):
    permits_retirement: ClassVar[bool] = True

    def require_retryable(self) -> None:
        pass

    def record_ready(self, attempt: AttemptRecord, db: sqlite3.Connection, note: str) -> None:
        attempt.advance(db, self, resolution=note)

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromFailedAttempt.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_failed_attempt()


class SucceededAttempt(FromClaimedAttempt, Terminal, ReadyAttempt, GoalAttemptPhase):
    verified_progress = True

    def record_ready(self, attempt: AttemptRecord, db: sqlite3.Connection, note: str) -> None:
        attempt.advance(db, self, progress_witness=note)


class ResolvedAttempt(FromReservedAttempt, FromClaimedAttempt, FromFailedAttempt, Terminal, GoalAttemptPhase):
    pass
