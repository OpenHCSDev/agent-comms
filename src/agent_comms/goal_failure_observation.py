"""Passive failed-attempt evidence. No retry, scheduling, or input authority.

The terminal caller supplies already-bound local evidence; this is not a tool
or owner-control ingress. Historical diagnostics cannot reconstruct a binding.
"""

from __future__ import annotations

import math
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

from .turn_failure import TurnFailure
from .field_codec import Id128Text
from .goal_attempt_identity import GoalAttemptIdentity
from .thread_identity import (
    ClaimedIdentity,
    RecordedThreadIncarnation,
    ThreadIncarnation,
    TurnFence,
    TurnIdentity,
)
from .typed_table import Column, DurableTables, ForeignKey, TypedTable

if TYPE_CHECKING:
    from .goal_attempts import Reservation
    from .goal_presentation import GoalExecution
    from .goals import Goal
    from .threads import Thread
    from .turn_lease import TurnLeaseFence


class GoalLedgerTable(DurableTables):
    """Declarations in the durable goal-attempt and passive evidence ledger."""


class GoalFailureEvidence(ABC):
    @abstractmethod
    def execution(self, goal_id: str) -> GoalExecution: ...


@dataclass(frozen=True)
class NoFailedTurnEvidence(GoalFailureEvidence):
    def execution(self, goal_id: str) -> GoalExecution:
        from .goal_presentation import AttemptUnresolvedExecution
        return AttemptUnresolvedExecution(goal_id)


@dataclass(frozen=True)
class FailedTurnEvidence(GoalLedgerTable, TypedTable, GoalFailureEvidence):
    attempt_id: Annotated[str, Column(primary_key=True)]
    goal_id: str
    generation: Annotated[int, Column(check="generation>0")]
    owner: str
    owner_created_at: float
    worktree: str
    claim_generation: Annotated[
        int, Column(record_key=True, check="{claim_generation}>0")
    ]
    turn_generation: Annotated[int, Column(check="turn_generation>0")]
    goal_revision: Annotated[int, Column(check="goal_revision>=0")]
    turn_id: Annotated[str, Column(unique=True, check="length(turn_id)=32")]
    failure: type[TurnFailure]

    def __post_init__(self) -> None:
        # These are the durable observation's constraints, independent of any
        # caller's authority. SQL/FieldCodec remains the single stored decoder.
        if type(self.claim_generation) is not int or self.claim_generation <= 0:
            raise ValueError("Observation claim_scope must be positive")
        if type(self.turn_generation) is not int or self.turn_generation <= 0:
            raise ValueError("Observation turn generation must be positive")
        if not math.isfinite(self.owner_created_at):
            raise ValueError("Observation incarnation must be finite")
        Id128Text.from_text(self.turn_id)

    def execution(self, goal_id: str) -> GoalExecution:
        from .goal_presentation import AttemptFailedExecution
        return AttemptFailedExecution(goal_id, self)

    @property
    def identity(self) -> GoalAttemptIdentity:
        return GoalAttemptIdentity(self.goal_id, self.generation, self.attempt_id)

    @property
    def incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.owner, self.owner_created_at)

    @property
    def claimed_identity(self) -> ClaimedIdentity:
        return ClaimedIdentity(self.incarnation, self.claim_generation)

    @property
    def turn(self) -> TurnFence:
        return TurnFence(TurnIdentity(self.incarnation, self.turn_generation), self.turn_id, self.claim_generation)

    def matches_owner(self, owner: Thread, claim_scope: int | None) -> bool:
        if claim_scope is None:
            return False
        if self.claimed_identity != ClaimedIdentity(owner.incarnation, claim_scope):
            return False
        if self.worktree != owner.worktree:
            return False
        if self.turn != owner.observed_turn(claim_scope):
            return False
        return owner.goal.accepts_observation(self.goal_id, self.goal_revision)

    @classmethod
    def references(cls):
        from .goal_attempts import AttemptRecord

        return (ForeignKey(columns=("attempt_id",), target=AttemptRecord, target_columns=("attempt_id",)),)


@dataclass(frozen=True)
class FailedTurnObservation:
    reservation: Reservation = field(repr=False)
    evidence: FailedTurnEvidence

    @classmethod
    def from_terminal(
        cls,
        reservation: Reservation,
        *,
        owner: Thread,
        goal: Goal,
        lease: TurnLeaseFence,
        turn_id: str,
        claim_scope: int,
        current_owner: Thread | None,
        current_claim: int | None,
        failure: type[TurnFailure],
    ) -> FailedTurnObservation | None:
        # Observation is passive: a changed owner, goal or lease yields no
        # observation. The evidence is built from already-trusted values, so a
        # construction error is a defect and raises.
        evidence = FailedTurnEvidence(
            reservation.attempt_id, reservation.goal_id, reservation.generation,
            owner.name, owner.incarnation.wire_birth, owner.worktree, claim_scope,
            lease.identity.generation, goal.revision, turn_id, failure,
        )
        if owner.goal != goal or reservation.goal_id != goal.id:
            return None
        if lease != evidence.turn:
            return None
        if current_owner is None:
            return None
        if current_owner.process_identity != owner.process_identity:
            return None
        if not evidence.matches_owner(current_owner, current_claim):
            return None
        return cls(reservation, evidence)


def record_observation(
    conn: sqlite3.Connection, reservation: Reservation, observation: FailedTurnObservation
) -> None:
    """Optional observation cannot roll back the enclosing failure fence.

    Savepoint errors which cannot be rolled back still escape to the store's
    normal StorageUncertainError path. They never yield a successful execution.
    """
    if observation.reservation != reservation or observation.evidence.identity != reservation.identity:
        return
    conn.execute("SAVEPOINT passive_observation")
    try:
        observation.evidence.insert(conn)
    except sqlite3.Error:
        conn.execute("ROLLBACK TO passive_observation")
    finally:
        conn.execute("RELEASE passive_observation")
