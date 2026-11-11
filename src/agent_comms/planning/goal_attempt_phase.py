from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from sqlite3 import Connection
from typing import ClassVar

from agent_comms.errors import UnresolvedAttemptError
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.lifecycle import LifecycleState
from agent_comms.nominal.lifecycle_capability import Failed, Terminal
from agent_comms.planning.attempt_resolution import AttemptResolution


class GoalAttemptTransition(ABC):
    __slots__ = ()

    @abstractmethod
    def advance(
        self, conn: Connection, phase: GoalAttemptPhase, *,
        progress_witness: str | None = ..., resolution: AttemptResolution = ...,
    ) -> None: ...


class ReadyAttempt[Evidence](ABC):
    """Attempt outcomes that record their own evidence before a fresh grant."""

    @abstractmethod
    def record_ready(
        self, attempt: GoalAttemptTransition, db: Connection, evidence: Evidence
    ) -> None: ...


@dataclass(frozen=True)
class GoalAttemptPhase(DeclaredFamily, LifecycleState, affix="Attempt"):
    launched: ClassVar[bool] = False
    verified_progress: ClassVar[bool] = False
    permits_retirement: ClassVar[bool] = False

    def require_retryable(self) -> None:
        raise UnresolvedAttemptError("Only a blocked, recorded failure may be retried explicitly.")


class ReservedAttempt(GoalAttemptPhase):
    @classmethod
    def successors(cls) -> tuple[type[GoalAttemptPhase], ...]:
        return ClaimedAttempt, FailedAttempt, ResolvedAttempt


class ClaimedAttempt(GoalAttemptPhase):
    launched = True

    @classmethod
    def successors(cls) -> tuple[type[GoalAttemptPhase], ...]:
        return FailedAttempt, SucceededAttempt, ResolvedAttempt


class FailedAttempt(Failed, ReadyAttempt[AttemptResolution], GoalAttemptPhase):
    permits_retirement: ClassVar[bool] = True

    def require_retryable(self) -> None:
        pass

    def record_ready(
        self, attempt: GoalAttemptTransition, db: Connection, resolution: AttemptResolution
    ) -> None:
        attempt.advance(db, self, resolution=resolution)

    @classmethod
    def successors(cls) -> tuple[type[GoalAttemptPhase], ...]:
        return (ResolvedAttempt,)


class SucceededAttempt(Terminal, ReadyAttempt[str], GoalAttemptPhase):
    verified_progress = True

    def record_ready(
        self, attempt: GoalAttemptTransition, db: Connection, progress_witness: str
    ) -> None:
        attempt.advance(db, self, progress_witness=progress_witness)


class ResolvedAttempt(Terminal, GoalAttemptPhase):
    pass
