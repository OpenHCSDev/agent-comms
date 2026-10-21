"""Private goal generation lifecycle; SQL fences and identities stay in their store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from agent_comms.goal_turn_permit import GoalTurnPermit, NoLaunchPermit

from .declared_family import DeclaredFamily
from .goal_attempt_phase import (
    FailedAttempt,
    GoalAttemptPhase,
)
from .lifecycle import LifecycleState
from .lifecycle_capability import Complete, Failed, Terminal

if TYPE_CHECKING:
    from .goal_actions import GoalActionContext
    from .goal_attempts import Generation, GoalAttemptStore
    from .goal_failure_observation import GoalFailureEvidence
    from .goal_management import Goals
    from .goal_presentation import GoalExecution, GoalExecutionSource
    from .goals import Goal
    from .threads import Thread


@dataclass(frozen=True)
class GenerationState(DeclaredFamily, LifecycleState, affix="Generation"):
    ready: ClassVar[bool] = False

    def resume_status(self, generation: Generation, goal: Goal, ctx: GoalActionContext) -> None:
        if not self.allows_resume(ctx.thread.executing):
            raise ValueError("The goal attempt is unresolved; inspect it before Retry.")

    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        return source.turn.execution(source)

    def idle_execution(self, source: GoalExecutionSource) -> GoalExecution:
        from .goal_presentation import AttemptUnresolvedExecution

        return AttemptUnresolvedExecution(source.goal.id)

    def evidence(self, store: GoalAttemptStore, generation: Generation) -> GoalFailureEvidence:
        from .goal_failure_observation import NoFailedTurnEvidence

        return NoFailedTurnEvidence()

    def continue_work(self, store: GoalAttemptStore, generation: Generation, witness: str) -> None:
        """An accepted order or verified progress does not resolve unknown work."""
        pass

    def turn_permit(
        self,
        store: GoalAttemptStore,
        goals: Goals,
        owner: Thread,
        claim_scope: int,
        generation: Generation,
    ) -> GoalTurnPermit:
        return NoLaunchPermit()

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


@dataclass(frozen=True)
class ReadyGeneration(GenerationState):
    ready = True

    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        from .goal_presentation import RunnableExecution

        return RunnableExecution(source.goal.id)

    def turn_permit(
        self,
        store: GoalAttemptStore,
        goals: Goals,
        owner: Thread,
        claim_scope: int,
        generation: Generation,
    ) -> GoalTurnPermit:
        grant = goals.ready_goal_grant_locked(owner, claim_scope, generation)
        return store.claim_launch(
            store.reserve(generation.goal_id, generation.number, ready_grant=grant)
        )

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
class BlockedGeneration(Failed, GenerationState):
    def resume_status(self, generation: Generation, goal: Goal, ctx: GoalActionContext) -> None:
        """A status-only Resume cannot authorize an uncertain attempt."""
        if generation.attempt_id:
            raise ValueError(
                "The interrupted goal attempt is unresolved. Inspect it, then use "
                "Retry to authorize a new attempt. Your messages can still be sent."
            )
        super().resume_status(generation, goal, ctx)

    def evidence(self, store: GoalAttemptStore, generation: Generation) -> GoalFailureEvidence:
        return store.failure_evidence(generation)

    def idle_execution(self, source: GoalExecutionSource) -> GoalExecution:
        return source.evidence.execution(source.goal.id)

    def continue_work(self, store: GoalAttemptStore, generation: Generation, witness: str) -> None:
        store.authorize_current_retry(generation.goal_id, witness)


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
    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        from .goal_presentation import CompletedExecution

        return CompletedExecution(source.goal.id)


@dataclass(frozen=True)
class CancelledGeneration(Terminal, GenerationState):
    def validate_attempt(self, attempt_id: str | None) -> None:
        if attempt_id is not None and not attempt_id:
            raise ValueError("A retired attempt identity must be nonempty.")
