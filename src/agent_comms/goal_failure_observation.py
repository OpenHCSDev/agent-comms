"""Passive failed-attempt evidence. No retry, scheduling, or input authority.

The terminal caller supplies already-bound local evidence; this is not a tool
or owner-control ingress. Historical diagnostics cannot reconstruct a binding.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .diagnostics import FailureReason
from .coordination_database import CoordinationStore
from .field_codec import FieldCodec, Id128Text
from .goal_attempt_identity import FailureNotObserved, GoalAttemptIdentity
from .thread_identity import ClaimedIdentity, RecordedThreadIncarnation, ThreadIncarnation, TurnFence, TurnIdentity
from .thread_status import ThreadStatus
from .typed_table import Column, ForeignKey, TypedTable

if TYPE_CHECKING:
    from .goal_attempts import Reservation
    from .goals import Goal
    from .threads import Thread
    from .turn_lease import TurnLeaseFence


class GoalLedgerTable:
    """Declarations in the durable goal-attempt and passive evidence ledger."""

    @classmethod
    def declared_schema(cls) -> dict[str, str]:
        return {
            name: sql
            for table in TypedTable.members_with(cls)
            for name, sql in table.schema_objects().items()
        }

    @classmethod
    def schema_digest(cls) -> str:
        """Identity of the actual declarations, including generated family checks."""
        return hashlib.sha256(
            json.dumps(cls.declared_schema(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class FailedTurnEvidence(GoalLedgerTable, TypedTable):
    attempt_id: str = field(metadata={"sql": Column(primary_key=True)})
    goal_id: str
    generation: int = field(metadata={"sql": Column(check="generation>0")})
    owner: str
    owner_created_at: float
    worktree: str
    claim_scope: int = field(metadata={"wire_name": 'admission', "sql": Column(record_key=True, check="{claim_scope}>0")})
    turn_generation: int = field(metadata={"sql": Column(check="turn_generation>0")})
    goal_revision: int = field(metadata={"sql": Column(check="goal_revision>=0")})
    turn_id: str = field(metadata={"sql": Column(unique=True, check="length(turn_id)=32")})
    reason: FailureReason

    def __post_init__(self) -> None:
        # These are the durable observation's constraints, independent of any
        # caller's authority. SQL/FieldCodec remains the single stored decoder.
        if type(self.claim_scope) is not int or self.claim_scope <= 0:
            raise ValueError("Observation claim_scope must be positive")
        if type(self.turn_generation) is not int or self.turn_generation <= 0:
            raise ValueError("Observation turn generation must be positive")
        if not math.isfinite(self.owner_created_at):
            raise ValueError("Observation incarnation must be finite")
        Id128Text.from_text(self.turn_id)
        if not isinstance(self.reason, FailureReason):
            raise ValueError("Observation reason must be decoded")

    @property
    def identity(self) -> GoalAttemptIdentity:
        return GoalAttemptIdentity(self.goal_id, self.generation, self.attempt_id)

    @property
    def incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.owner, self.owner_created_at)

    @property
    def claimed_identity(self) -> ClaimedIdentity:
        return ClaimedIdentity(self.incarnation, self.claim_scope)

    @property
    def turn(self) -> TurnFence:
        return TurnFence(TurnIdentity(self.incarnation, self.turn_generation), self.turn_id, self.claim_scope)

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

        return (ForeignKey(("attempt_id",), AttemptRecord, ("attempt_id",)),)


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
        reason: FailureReason,
    ) -> FailedTurnObservation | None:
        # Observation is passive: a changed owner, goal or lease yields no
        # observation. The evidence is built from already-trusted values, so a
        # construction error is a defect and raises.
        evidence = FailedTurnEvidence(
            reservation.attempt_id, reservation.goal_id, reservation.generation,
            owner.name, owner.created_at, owner.worktree, claim_scope,
            lease.identity.generation, goal.revision, turn_id, reason,
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


ObservationState = Literal["backend_suspended", "owner_paused", "paused_uncertain", "unavailable"]


@dataclass(frozen=True, slots=True)
class FailedTurnProjection:
    state: ObservationState
    reason: str

    def to_primitive(self) -> dict[str, object]:
        # Deliberately no incident IDs, token, worktree, turn ID, diagnostics,
        # acknowledgement bodies, canRetry, grants, or control actions.
        return {"schema": 1, **FieldCodec.encode(self)}

    @classmethod
    def missing_store(cls, store, goal, owner, claim_scope):
        return cls("unavailable", store.failure_reason)

    @classmethod
    def invalid_store(cls, store, goal, owner, claim_scope):
        return cls("unavailable", store.failure_reason)

    @classmethod
    def capture(cls, conn, goal, owner, claim_scope):
        from .goal_attempts import (
            AttemptRecord,
            Generation,
            StorageUncertainError,
            assert_goal_attempt_schema,
        )

        try:
            assert_goal_attempt_schema(conn)
        except StorageUncertainError:
            return cls("unavailable", "unsupported_schema")
        generation = Generation.one(conn, goal_id=goal.id)
        if generation is None:
            return cls("unavailable", "missing_binding")
        try:
            expected = generation.failure_identity()
            attempt = AttemptRecord.one(conn, attempt_id=expected.attempt_id)
            if attempt is None:
                return cls("unavailable", "missing_binding")
            attempt.phase.require_failure()
        except FailureNotObserved as error:
            return cls("unavailable", str(error))
        row = FailedTurnEvidence.one(conn, attempt_id=expected.attempt_id)
        if row is None:
            return cls("unavailable", "missing_binding")
        if row.identity != expected or attempt.reservation.identity != expected:
            return cls("unavailable", "missing_binding")
        if not row.matches_owner(owner, claim_scope):
            return cls("unavailable", "owner_or_goal_changed")
        state, explanation = goal.state.failure_projection(row.reason.value)
        return cls(state, explanation)

    @classmethod
    def read_for_goal(cls, path: Path, owner: Thread, owner_status: ThreadStatus,
                      claim_scope: int, goal: Goal) -> FailedTurnProjection:
        if not owner.process_identity.allows_goal_observation(owner_status):
            return cls("unavailable", "owner_or_goal_changed")
        try:
            # Read lifecycle eligibility before opening the passive ledger.
            goal.state.failure_projection("")
        except FailureNotObserved as error:
            return cls("unavailable", str(error))
        if type(claim_scope) is not int or claim_scope <= 0:
            return cls("unavailable", "owner_or_goal_changed")
        with CoordinationStore.observed(path).observing(cls, goal, owner, claim_scope) as result:
            return result


def read_failed_turn_projection(
    path: Path,
    *,
    owner: Thread,
    owner_status: ThreadStatus,
    claim_scope: int,
) -> FailedTurnProjection:
    """Read the existing private goal ledger, never initialize/migrate/repair it.

    Caller supplies trusted canonical registry snapshots, not a viewer's
    claimed identity. Samples are not an atomic cross-store revision and must
    never be used for dispatch or as evidence of absence of an owner pause.
    """

    return owner.goal.failed_turn_projection(path, owner, owner_status, claim_scope)
