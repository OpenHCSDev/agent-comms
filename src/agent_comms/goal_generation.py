"""Private goal generation lifecycle; SQL fences and identities stay in their store."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .goal_attempt_phase import (
    FailedAttempt,
    GoalAttemptPhase,
)
from .lifecycle import LifecycleState

if TYPE_CHECKING:
    from .goal_attempts import Generation, GoalAttemptStore


@dataclass(frozen=True)
class GenerationState(DeclaredFamily, LifecycleState, affix="Generation"):
    terminal: ClassVar[bool] = False
    ready: ClassVar[bool] = False
    failed: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[GenerationState], ...]: ...

    def validate_attempt(self, attempt_id: str | None) -> None:
        if not attempt_id:
            raise ValueError("This generation requires an attempt identity.")

    def permits_retirement(self, phase: GoalAttemptPhase | None) -> bool:
        return False

    def allows_resume(self, active_turn: bool) -> bool:
        return False

    def authorize_retry(
        self, store: GoalAttemptStore, generation: Generation, decision: str
    ) -> None:
        raise ValueError("The goal attempt is unresolved; inspect it before retrying.")


@dataclass(frozen=True)
class ReadyGeneration(GenerationState):
    ready = True

    def validate_attempt(self, attempt_id: str | None) -> None:
        if attempt_id is not None:
            raise ValueError("READY cannot carry an attempt identity.")

    def permits_retirement(self, phase: GoalAttemptPhase | None) -> bool:
        return phase is None

    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ReadyGeneration, ReservedGeneration, CancelledGeneration

    def allows_resume(self, active_turn: bool) -> bool:
        return True

    def authorize_retry(
        self, store: GoalAttemptStore, generation: Generation, decision: str
    ) -> None:
        store.authorize_ready_recovery(
            generation.goal_id, expected_generation=generation.number, user_decision_id=decision
        )


@dataclass(frozen=True)
class ReservedGeneration(GenerationState):
    def permits_retirement(self, phase: GoalAttemptPhase | None) -> bool:
        return phase is not None and phase.may_become(FailedAttempt())

    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ReadyGeneration, BlockedGeneration, CompletedGeneration, CancelledGeneration

    def allows_resume(self, active_turn: bool) -> bool:
        return active_turn


@dataclass(frozen=True)
class BlockedGeneration(GenerationState):
    failed = True

    def permits_retirement(self, phase: GoalAttemptPhase | None) -> bool:
        return isinstance(phase, FailedAttempt)

    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ReadyGeneration, CancelledGeneration

    def authorize_retry(
        self, store: GoalAttemptStore, generation: Generation, decision: str
    ) -> None:
        assert generation.attempt_id is not None
        store.authorize_retry(
            generation.goal_id,
            expected_generation=generation.number,
            attempt_id=generation.attempt_id,
            user_decision_id=decision,
        )


@dataclass(frozen=True)
class CompletedGeneration(GenerationState):
    terminal = True

    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ()


@dataclass(frozen=True)
class CancelledGeneration(GenerationState):
    terminal = True

    def validate_attempt(self, attempt_id: str | None) -> None:
        if attempt_id is not None and not attempt_id:
            raise ValueError("A retired attempt identity must be nonempty.")

    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ()
