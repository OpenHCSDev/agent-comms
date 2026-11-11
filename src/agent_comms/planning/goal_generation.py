from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from sqlite3 import Connection
from typing import TYPE_CHECKING, ClassVar

from agent_comms.errors import GoalRefused, UnresolvedAttemptError
from agent_comms.execution.goal_turn_permit import GoalTurnPermit, NoLaunchPermit
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import AttemptId, NoAttemptId
from agent_comms.nominal.lifecycle import LifecycleState
from agent_comms.nominal.lifecycle_capability import Complete, Failed, Terminal
from agent_comms.planning.attempt_resolution import WorkDecision
from agent_comms.planning.goal_attempt_phase import FailedAttempt, GoalAttemptPhase
from agent_comms.planning.goal_failure_observation import (
    GenerationFailureSource,
    GoalFailureEvidence,
    NoFailedTurnEvidence,
)
from agent_comms.planning.goal_mutation import (
    GoalAttemptRead,
    GoalGenerationChanges,
    GoalGenerationGrants,
    GoalGenerationProjection,
    GoalGenerationRead,
    GoalReadyGrantSource,
)


@dataclass(frozen=True)
class GenerationState(DeclaredFamily, LifecycleState, affix="Generation"):
    ready: ClassVar[bool] = False

    def resume_status(self, active_turn: bool) -> None:
        if not self.allows_resume(active_turn):
            raise GoalRefused("The goal attempt is unresolved; inspect it before Retry.")

    def execution[Execution](self, source: GoalGenerationProjection[Execution]) -> Execution:
        return source.turn_execution()

    def idle_execution[Execution](self, source: GoalGenerationProjection[Execution]) -> Execution:
        return source.unresolved()

    def evidence[Failure](
        self, connection: Connection, generation: GenerationFailureSource[Failure],
    ) -> GoalFailureEvidence[Failure]:
        return NoFailedTurnEvidence[Failure]()

    def continue_work(
        self, store: GoalGenerationChanges, generation: GoalGenerationRead, witness: WorkDecision,
    ) -> None:
        """An accepted order or verified progress does not resolve unknown work."""
        pass

    def turn_permit[Owner, Generation: GoalGenerationRead, Reservation](
        self, store: GoalGenerationGrants[Reservation],
        goals: GoalReadyGrantSource[Owner, Generation], owner: Owner, claim_scope: int,
        generation: Generation,
    ) -> GoalTurnPermit:
        return NoLaunchPermit()

    def validate_attempt(self, attempt_id: AttemptId | NoAttemptId) -> None:
        attempt_id.require_present()

    def require_ready(self) -> None:
        raise UnresolvedAttemptError(UnresolvedAttemptError.diagnostic_text)

    def permits_retirement_without_attempt(self) -> bool:
        return False

    def permits_retirement(self, phase: GoalAttemptPhase) -> bool:
        return False

    def allows_resume(self, active_turn: bool) -> bool:
        return False

    def stopped_permit[Reservation](
        self, store: GoalAttemptRead[Reservation], generation: GoalGenerationRead,
        make_permit: Callable[[Reservation], GoalTurnPermit],
    ) -> GoalTurnPermit:
        return NoLaunchPermit()

    @abstractmethod
    def authorize_retry(
        self, store: GoalGenerationChanges, generation: GoalGenerationRead, decision: WorkDecision,
    ) -> None:
        """Authorize new work or refuse according to this generation's lifecycle."""


@dataclass(frozen=True)
class ReadyGeneration(GenerationState):
    ready = True

    def execution[Execution](self, source: GoalGenerationProjection[Execution]) -> Execution:
        return source.runnable()

    def turn_permit[Owner, Generation: GoalGenerationRead, Reservation](
        self, store: GoalGenerationGrants[Reservation],
        goals: GoalReadyGrantSource[Owner, Generation], owner: Owner, claim_scope: int,
        generation: Generation,
    ) -> GoalTurnPermit:
        grant = goals.ready_goal_grant_locked(owner, claim_scope, generation)
        return store.claim_launch(
            store.reserve(generation.goal_id, generation.number, ready_grant=grant)
        )

    def require_ready(self) -> None:
        pass

    def validate_attempt(self, attempt_id: AttemptId | NoAttemptId) -> None:
        attempt_id.require_absent()

    def permits_retirement_without_attempt(self) -> bool:
        return True

    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ReadyGeneration, ReservedGeneration, CancelledGeneration

    def allows_resume(self, active_turn: bool) -> bool:
        return True

    def authorize_retry(
        self, store: GoalGenerationChanges, generation: GoalGenerationRead, decision: WorkDecision,
    ) -> None:
        store.authorize_ready_recovery(
            generation.goal_id, expected_generation=generation.number, decision=decision,
        )


@dataclass(frozen=True)
class ReservedGeneration(GenerationState):
    def authorize_retry(
        self, store: GoalGenerationChanges, generation: GoalGenerationRead, decision: WorkDecision,
    ) -> None:
        raise GoalRefused(
            f"The goal generation is reserved for attempt {generation.attempt_identity.attempt_id}; "
            "Retry cannot replace it. Inspect that attempt before deciding on new work."
        )

    def stopped_permit[Reservation](
        self, store: GoalAttemptRead[Reservation], generation: GoalGenerationRead,
        make_permit: Callable[[Reservation], GoalTurnPermit],
    ) -> GoalTurnPermit:
        return store.read_attempt(generation.attempt_identity.attempt_id).stopped_permit(make_permit)

    def permits_retirement(self, phase: GoalAttemptPhase) -> bool:
        return phase.may_become(FailedAttempt())

    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ReadyGeneration, BlockedGeneration, CompletedGeneration, CancelledGeneration

    def allows_resume(self, active_turn: bool) -> bool:
        return active_turn


@dataclass(frozen=True)
class BlockedGeneration(Failed, GenerationState):
    def resume_status(self, active_turn: bool) -> None:
        """A status-only Resume cannot authorize an uncertain attempt."""
        raise GoalRefused(
            "The interrupted goal attempt is unresolved. Inspect it, then use "
            "Retry to authorize a new attempt. Your messages can still be sent."
        )

    def evidence[Failure](
        self, connection: Connection, generation: GenerationFailureSource[Failure],
    ) -> GoalFailureEvidence[Failure]:
        return generation.failure_evidence(connection)

    def execution[Execution](self, source: GoalGenerationProjection[Execution]) -> Execution:
        """No permit launches a blocked attempt, so a running turn is never the goal's own."""
        return source.failed_execution()

    def continue_work(
        self, store: GoalGenerationChanges, generation: GoalGenerationRead, witness: WorkDecision,
    ) -> None:
        store.authorize_current_retry(generation.goal_id, witness)

    def permits_retirement(self, phase: GoalAttemptPhase) -> bool:
        return phase.permits_retirement

    @classmethod
    def successors(cls) -> tuple[type[GenerationState], ...]:
        return ReadyGeneration, CancelledGeneration

    def authorize_retry(
        self, store: GoalGenerationChanges, generation: GoalGenerationRead, decision: WorkDecision,
    ) -> None:
        identity = generation.attempt_identity
        store.authorize_retry(
            identity.goal_id, expected_generation=identity.generation,
            attempt_id=identity.attempt_id, decision=decision,
        )


@dataclass(frozen=True)
class CompletedGeneration(Complete, Terminal, GenerationState):
    def authorize_retry(
        self, store: GoalGenerationChanges, generation: GoalGenerationRead, decision: WorkDecision,
    ) -> None:
        raise GoalRefused("The goal generation is completed; Retry cannot restart completed work.")

    def execution[Execution](self, source: GoalGenerationProjection[Execution]) -> Execution:
        return source.completed()


@dataclass(frozen=True)
class CancelledGeneration(Terminal, GenerationState):
    def authorize_retry(
        self, store: GoalGenerationChanges, generation: GoalGenerationRead, decision: WorkDecision,
    ) -> None:
        raise GoalRefused("The goal generation is cancelled; Retry cannot restart cancelled work.")

    def validate_attempt(self, attempt_id: AttemptId | NoAttemptId) -> None:
        """Retirement preserves either the recorded attempt or its declared absence."""
