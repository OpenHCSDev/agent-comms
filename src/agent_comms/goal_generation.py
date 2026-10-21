"""Private goal generation lifecycle; SQL fences and identities stay in their store."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from agent_comms.goal_turn_permit import GoalTurnPermit, NoLaunchPermit

from .declared_family import DeclaredFamily
from .goal_attempt_phase import (
    FailedAttempt,
    GoalAttemptPhase,
)
from .goal_states import BlockedGoal
from .lifecycle import LifecycleState
from .lifecycle_capability import Complete, Failed, Terminal

if TYPE_CHECKING:
    from .goal_actions import GoalActionContext
    from .goal_attempts import Generation, GoalAttemptStore
    from .goals import Goal


@dataclass(frozen=True)
class GenerationState(DeclaredFamily, LifecycleState, affix="Generation"):
    ready: ClassVar[bool] = False

    def resume_status(self, generation: Generation, goal: Goal, ctx: GoalActionContext) -> None:
        if not self.allows_resume(ctx.thread.executing):
            raise ValueError("The goal attempt is unresolved; inspect it before Retry.")

    def validate_attempt(self, attempt_id: str | None) -> None:
        if not attempt_id:
            raise ValueError("This generation requires an attempt identity.")

    def require_ready(self) -> None:
        from .goal_attempts import UnresolvedAttemptError

        raise UnresolvedAttemptError(UnresolvedAttemptError.diagnostic_text)

    def permits_retirement(self, phase: GoalAttemptPhase | None) -> bool:
        return False

    def allows_resume(self, active_turn: bool) -> bool:
        return False

    def stopped_permit(self, store: GoalAttemptStore, generation: Generation) -> GoalTurnPermit:
        return NoLaunchPermit()

    def authorize_retry(
        self, store: GoalAttemptStore, generation: Generation, decision: str
    ) -> None:
        raise ValueError("The goal attempt is unresolved; inspect it before retrying.")


class RetryableGeneration:
    """A generation an explicit retry or a fresh order may proceed from; every other
    generation's attempt is unresolved until someone inspects it."""


@dataclass(frozen=True)
class ReadyGeneration(GenerationState, RetryableGeneration):
    ready = True

    def require_ready(self) -> None:
        pass

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
    def stopped_permit(self, store: GoalAttemptStore, generation: Generation) -> GoalTurnPermit:
        return store.read_attempt(generation.attempt_identity.attempt_id).stopped_permit()

    def permits_retirement(self, phase: GoalAttemptPhase | None) -> bool:
        return phase is not None and phase.may_become(FailedAttempt())

    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ReadyGeneration, BlockedGeneration, CompletedGeneration, CancelledGeneration

    def allows_resume(self, active_turn: bool) -> bool:
        return active_turn


@dataclass(frozen=True)
class BlockedGeneration(Failed, GenerationState, RetryableGeneration):
    def resume_status(self, generation: Generation, goal: Goal, ctx: GoalActionContext) -> None:
        if generation.attempt_id:
            state = BlockedGoal(
                "The interrupted goal attempt is unresolved. Inspect it, then use "
                "Retry to authorize a new attempt. Your messages can still be sent."
            )
            blocked = replace(goal, state=state, revision=goal.revision + 1)
            ctx.goals.registry.register(
                replace(ctx.thread, goal=blocked), ctx.goals.registry.status(ctx.thread.name)
            )
            raise ValueError(state.reason)
        super().resume_status(generation, goal, ctx)

    def permits_retirement(self, phase: GoalAttemptPhase | None) -> bool:
        return isinstance(phase, FailedAttempt)

    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ReadyGeneration, CancelledGeneration

    def authorize_retry(
        self, store: GoalAttemptStore, generation: Generation, decision: str
    ) -> None:
        identity = generation.attempt_identity
        store.authorize_retry(
            identity.goal_id,
            expected_generation=identity.generation,
            attempt_id=identity.attempt_id,
            user_decision_id=decision,
        )


@dataclass(frozen=True)
class CompletedGeneration(Complete, Terminal, GenerationState):
    pass


@dataclass(frozen=True)
class CancelledGeneration(Terminal, GenerationState):
    def validate_attempt(self, attempt_id: str | None) -> None:
        if attempt_id is not None and not attempt_id:
            raise ValueError("A retired attempt identity must be nonempty.")
