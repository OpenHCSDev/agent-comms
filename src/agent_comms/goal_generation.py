"""Private goal generation lifecycle; SQL fences and identities stay in their store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from agent_comms.goal_turn_permit import GoalTurnPermit, NoLaunchPermit

from .declared_family import DeclaredFamily
from .errors import UnresolvedAttemptError
from .goal_attempt_phase import (
    FailedAttempt,
    GoalAttemptPhase,
)
from .goal_failure_observation import NoFailedTurnEvidence
from .goal_presentation import (
    AttemptUnresolvedExecution,
    CompletedExecution,
    RunnableExecution,
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


class FromReadyGeneration:
    @classmethod
    def after_ready_generation(cls) -> bool:
        return True


class FromReservedGeneration:
    @classmethod
    def after_reserved_generation(cls) -> bool:
        return True


class FromBlockedGeneration:
    @classmethod
    def after_blocked_generation(cls) -> bool:
        return True



@dataclass(frozen=True)
class GenerationState(DeclaredFamily, LifecycleState, affix="Generation"):
    ready: ClassVar[bool] = False

    def resume_status(self, generation: Generation, goal: Goal, ctx: GoalActionContext) -> None:
        if not self.allows_resume(ctx.thread.executing):
            raise ValueError("The goal attempt is unresolved; inspect it before Retry.")

    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        return source.turn.execution(source)

    def idle_execution(self, source: GoalExecutionSource) -> GoalExecution:
        return AttemptUnresolvedExecution(source.goal.id)

    def evidence(self, store: GoalAttemptStore, generation: Generation) -> GoalFailureEvidence:
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
        raise UnresolvedAttemptError(UnresolvedAttemptError.diagnostic_text)

    def permits_retirement_without_attempt(self) -> bool:
        return False

    def permits_retirement(self, phase: GoalAttemptPhase) -> bool:
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
class ReadyGeneration(FromReadyGeneration, FromReservedGeneration, FromBlockedGeneration, GenerationState):
    ready = True

    def execution(self, source: GoalExecutionSource) -> GoalExecution:
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

    def permits_retirement_without_attempt(self) -> bool:
        return True

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromReadyGeneration.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_ready_generation()

    def allows_resume(self, active_turn: bool) -> bool:
        return True

    def authorize_retry(
        self, store: GoalAttemptStore, generation: Generation, decision: str
    ) -> None:
        store.authorize_ready_recovery(
            generation.goal_id, expected_generation=generation.number, user_decision_id=decision
        )


@dataclass(frozen=True)
class ReservedGeneration(FromReadyGeneration, GenerationState):
    def stopped_permit(self, store: GoalAttemptStore, generation: Generation) -> GoalTurnPermit:
        return store.read_attempt(generation.attempt_identity.attempt_id).stopped_permit()

    def permits_retirement(self, phase: GoalAttemptPhase) -> bool:
        return phase.may_become(FailedAttempt())

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromReservedGeneration.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_reserved_generation()

    def allows_resume(self, active_turn: bool) -> bool:
        return active_turn


@dataclass(frozen=True)
class BlockedGeneration(FromReservedGeneration, Failed, GenerationState):
    def resume_status(self, generation: Generation, goal: Goal, ctx: GoalActionContext) -> None:
        """A status-only Resume cannot authorize an uncertain attempt."""
        if generation.attempt_id:
            raise ValueError(
                "The interrupted goal attempt is unresolved. Inspect it, then use "
                "Retry to authorize a new attempt. Your messages can still be sent."
            )
        super().resume_status(generation, goal, ctx)

    def evidence(self, store: GoalAttemptStore, generation: Generation) -> GoalFailureEvidence:
        return generation.failure_evidence(store)

    def idle_execution(self, source: GoalExecutionSource) -> GoalExecution:
        return source.evidence.execution(source.goal.id)

    def continue_work(self, store: GoalAttemptStore, generation: Generation, witness: str) -> None:
        store.authorize_current_retry(generation.goal_id, witness)


    def permits_retirement(self, phase: GoalAttemptPhase) -> bool:
        return phase.permits_retirement

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromBlockedGeneration.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_blocked_generation()

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
class CompletedGeneration(FromReservedGeneration, Complete, Terminal, GenerationState):
    def execution(self, source: GoalExecutionSource) -> GoalExecution:
        return CompletedExecution(source.goal.id)


@dataclass(frozen=True)
class CancelledGeneration(FromReadyGeneration, FromReservedGeneration, FromBlockedGeneration, Terminal, GenerationState):
    def validate_attempt(self, attempt_id: str | None) -> None:
        if attempt_id is not None and not attempt_id:
            raise ValueError("A retired attempt identity must be nonempty.")
