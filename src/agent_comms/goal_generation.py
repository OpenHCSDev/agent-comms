"""Private goal generation lifecycle; SQL fences and identities stay in their store."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
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

    def allows_resume(self, active_turn: bool) -> bool:
        return False

    def authorize_retry(
        self, store: GoalAttemptStore, generation: Generation, decision: str
    ) -> None:
        raise ValueError("The goal attempt is unresolved; inspect it before retrying.")


@dataclass(frozen=True)
class ReadyGeneration(GenerationState):
    ready = True

    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ReservedGeneration, CancelledGeneration

    def allows_resume(self, active_turn: bool) -> bool:
        return True

    def authorize_retry(
        self, store: GoalAttemptStore, generation: Generation, decision: str
    ) -> None:
        if generation.attempt_id is not None:
            return super().authorize_retry(store, generation, decision)
        store.authorize_ready_recovery(
            generation.goal_id, expected_generation=generation.number, user_decision_id=decision
        )


@dataclass(frozen=True)
class ReservedGeneration(GenerationState):
    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ReadyGeneration, BlockedGeneration, CompletedGeneration, CancelledGeneration

    def allows_resume(self, active_turn: bool) -> bool:
        return active_turn


@dataclass(frozen=True)
class BlockedGeneration(GenerationState):
    failed = True

    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ReadyGeneration, CancelledGeneration

    def authorize_retry(
        self, store: GoalAttemptStore, generation: Generation, decision: str
    ) -> None:
        if not generation.attempt_id:
            return super().authorize_retry(store, generation, decision)
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

    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ()
