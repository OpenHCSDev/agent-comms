"""Passive failed-attempt evidence. No retry, scheduling, or input authority.

The terminal caller supplies already-bound local evidence; this is not a tool
or owner-control ingress. Historical diagnostics cannot reconstruct a binding.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .diagnostics import FailureReason
from .field_codec import FieldCodec
from .goal_attempt_identity import FailureNotObserved, GoalAttemptIdentity
from .goals import Goal
from .recovery_projection import _preflight
from .thread_identity import AdmissionIdentity, ThreadIncarnation, TurnIdentity
from .thread_status import ThreadStatus
from .threads import Thread
from .turn_lease import TurnFence, TurnLeaseFence
from .typed_table import Column, ForeignKey, TypedTable

if TYPE_CHECKING:
    from .goal_attempts import Reservation


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
    admission: int = field(metadata={"sql": Column(check="admission>0")})
    turn_generation: int = field(metadata={"sql": Column(check="turn_generation>0")})
    goal_revision: int = field(metadata={"sql": Column(check="goal_revision>=0")})
    turn_id: str = field(metadata={"sql": Column(unique=True, check="length(turn_id)=32")})
    reason: FailureReason

    def __post_init__(self) -> None:
        # These are the durable observation's constraints, independent of any
        # caller's authority. SQL/FieldCodec remains the single stored decoder.
        if type(self.admission) is not int or self.admission <= 0:
            raise ValueError("Observation admission must be positive")
        if type(self.turn_generation) is not int or self.turn_generation <= 0:
            raise ValueError("Observation turn generation must be positive")
        if not math.isfinite(self.owner_created_at):
            raise ValueError("Observation incarnation must be finite")
        if not re.fullmatch(r"[0-9a-f]{32}", self.turn_id):
            raise ValueError("Observation turn ID must be canonical")
        if not isinstance(self.reason, FailureReason):
            raise ValueError("Observation reason must be decoded")

    @property
    def identity(self) -> GoalAttemptIdentity:
        return GoalAttemptIdentity(self.goal_id, self.generation, self.attempt_id)

    @property
    def incarnation(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.owner, self.owner_created_at)

    @property
    def admission_identity(self) -> AdmissionIdentity:
        return AdmissionIdentity(self.incarnation, self.admission)

    @property
    def turn(self) -> TurnFence:
        return TurnFence(TurnIdentity(self.incarnation, self.turn_generation), self.turn_id, self.admission)

    def matches_owner(self, owner: Thread, admission: int | None) -> bool:
        if admission is None:
            return False
        if self.admission_identity != AdmissionIdentity(owner.incarnation, admission):
            return False
        if self.worktree != owner.worktree:
            return False
        observed = owner.observed_turn(admission)
        if observed is None or not self.turn.matches(observed):
            return False
        return owner.has_goal_revision(self.goal_id, self.goal_revision)

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
        admission: int,
        current_owner: Thread | None,
        current_admission: int | None,
        reason: FailureReason,
    ) -> FailedTurnObservation | None:
        # Observation is passive. Its failure never changes reservation
        # disposition or makes the enclosing failure settlement optional.
        try:
            evidence = FailedTurnEvidence(
                reservation.attempt_id, reservation.goal_id, reservation.generation,
                owner.name, owner.created_at, owner.worktree, admission,
                lease.identity.generation, goal.revision, turn_id, reason,
            )
        except (ValueError, TypeError):
            return None
        if owner.goal != goal or reservation.goal_id != goal.id:
            return None
        if not lease.matches(evidence.turn):
            return None
        if current_owner is None:
            return None
        if current_owner.process_identity != owner.process_identity:
            return None
        if not evidence.matches_owner(current_owner, current_admission):
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
        # receipt bodies, canRetry, grants, or control actions.
        return {"schema": 1, **FieldCodec.encode(self)}


def read_failed_turn_projection(
    path: Path,
    *,
    owner: Thread,
    owner_status: ThreadStatus,
    admission: int,
) -> FailedTurnProjection:
    """Read the existing private goal ledger, never initialize/migrate/repair it.

    Caller supplies trusted canonical registry snapshots, not a viewer's
    claimed identity. Samples are not an atomic cross-store revision and must
    never be used for dispatch or as evidence of absence of an owner pause.
    """

    def unavailable(reason: str) -> FailedTurnProjection:
        return FailedTurnProjection("unavailable", reason)

    goal = owner.goal
    if goal is None or not owner_status.active or owner.pid <= 0:
        return unavailable("owner_or_goal_changed")
    try:
        # Resolve lifecycle eligibility before reading the ledger; active goals
        # must not surface a historical failure, even from a damaged store.
        goal.state.failure_projection("")
    except FailureNotObserved as error:
        return unavailable(str(error))
    if type(admission) is not int or admission <= 0:
        return unavailable("owner_or_goal_changed")
    if failure := _preflight(path):
        return unavailable(failure)
    try:
        with closing(
            sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=0.25
            )
        ) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            try:
                from .goal_attempts import (
                    AttemptRecord,
                    Generation,
                    StorageUncertainError,
                    assert_goal_attempt_schema,
                )

                try:
                    assert_goal_attempt_schema(conn)
                except StorageUncertainError:
                    return unavailable("unsupported_schema")
                generation = Generation.one(conn, goal_id=goal.id)
                if generation is None:
                    return unavailable("missing_binding")
                expected = generation.failure_identity()
                attempt = AttemptRecord.one(conn, attempt_id=expected.attempt_id)
                if attempt is None:
                    return unavailable("missing_binding")
                attempt.phase.require_failure()
                row = FailedTurnEvidence.one(conn, attempt_id=expected.attempt_id)
                if row is None:
                    return unavailable("missing_binding")
                if row.identity != expected or attempt.reservation.identity != expected:
                    return unavailable("missing_binding")
                if not row.matches_owner(owner, admission):
                    return unavailable("owner_or_goal_changed")
                reason = row.reason.value
            finally:
                conn.execute("ROLLBACK")
    except FailureNotObserved as error:
        return unavailable(str(error))
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return unavailable("invalid_store")
    state, explanation = goal.state.failure_projection(reason)
    return FailedTurnProjection(state, explanation)
