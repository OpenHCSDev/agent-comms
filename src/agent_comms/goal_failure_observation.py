"""Passive failed-attempt evidence. No retry, scheduling, or input authority.

The terminal caller supplies already-bound local evidence; this is not a tool
or owner-control ingress. Historical diagnostics cannot reconstruct a binding.
"""

from __future__ import annotations

import math
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .diagnostics import FailureReason
from .field_codec import FieldCodec
from .goal_attempt_phase import FailedAttempt
from .goal_generation import BlockedGeneration
from .goal_pauses import GoalPauseEvent
from .goal_states import BlockedState, PausedGoal
from .goals import Goal
from .recovery_projection import _preflight
from .thread_status import ThreadStatus
from .threads import Thread
from .turn_lease import TurnLeaseFence
from .typed_table import Column, ForeignKey, TypedTable

if TYPE_CHECKING:
    from .goal_attempts import Reservation


class GoalLedgerTable:
    """Declarations in the durable goal-attempt and passive evidence ledger."""


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
        # A mismatched observation is omitted, never repaired by name/time or
        # made a reason to skip the existing failed-attempt write.
        if (
            current_owner is None
            or current_owner.name != owner.name
            or current_owner.created_at != owner.created_at
            or current_owner.worktree != owner.worktree
            or current_owner.process_identity != owner.process_identity
            or current_owner.goal is None
            or current_owner.goal.id != goal.id
            or current_owner.goal.revision < goal.revision
            or current_owner.turn_generation != lease.identity.generation
            or (
                current_owner.active_turn.id
                if current_owner.active_turn is not None
                else current_owner.last_finished_turn_id
            )
            != turn_id
            or current_admission != admission
            or owner.goal != goal
            or reservation.goal_id != goal.id
            or lease.identity.incarnation != owner.incarnation
            or lease.turn_id != turn_id
            or lease.admission_generation != admission
            or type(admission) is not int
            or admission <= 0
            or type(lease.identity.generation) is not int
            or lease.identity.generation <= 0
            or not math.isfinite(owner.created_at)
            or not re.fullmatch(r"[0-9a-f]{32}", turn_id)
            or not isinstance(reason, FailureReason)
        ):
            return None
        return cls(
            reservation,
            FailedTurnEvidence(
                reservation.attempt_id,
                reservation.goal_id,
                reservation.generation,
                owner.name,
                owner.created_at,
                owner.worktree,
                admission,
                lease.identity.generation,
                goal.revision,
                turn_id,
                reason,
            ),
        )


def record_observation(
    conn: sqlite3.Connection, reservation: Reservation, observation: FailedTurnObservation
) -> None:
    """Optional observation cannot roll back the enclosing failure fence.

    Savepoint errors which cannot be rolled back still escape to the store's
    normal StorageUncertainError path. They never yield a successful execution.
    """
    if observation.reservation != reservation or (
        observation.evidence.attempt_id,
        observation.evidence.goal_id,
        observation.evidence.generation,
    ) != (reservation.attempt_id, reservation.goal_id, reservation.generation):
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
    pause: GoalPauseEvent | None,
) -> FailedTurnProjection:
    """Read the existing private goal ledger, never initialize/migrate/repair it.

    Caller supplies trusted canonical registry/pause snapshots, not a viewer's
    claimed identity. Samples are not an atomic cross-store revision and must
    never be used for dispatch or as evidence of absence of an owner pause.
    """

    def unavailable(reason: str) -> FailedTurnProjection:
        return FailedTurnProjection("unavailable", reason)

    goal = owner.goal
    if (
        not isinstance(owner_status, ThreadStatus)
        or not owner_status.active
        or owner.pid <= 0
        or goal is None
        or not isinstance(goal.state, (BlockedState, PausedGoal))
    ):
        return unavailable("owner_or_goal_changed")
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
                if generation is None or not isinstance(generation.lifecycle, BlockedGeneration):
                    return unavailable("missing_binding")
                attempt = AttemptRecord.one(conn, attempt_id=generation.attempt_id)
                if attempt is None or not isinstance(attempt.phase, FailedAttempt):
                    return unavailable("missing_binding")
                row = FailedTurnEvidence.one(conn, attempt_id=generation.attempt_id)
                if row is None or (row.goal_id, row.generation) != (goal.id, generation.number):
                    return unavailable("missing_binding")
                if (attempt.reservation.goal_id, attempt.reservation.generation) != (
                    goal.id,
                    generation.number,
                ):
                    return unavailable("missing_binding")
                if (
                    (row.owner, row.owner_created_at, row.worktree, row.admission)
                    != (owner.name, owner.created_at, owner.worktree, admission)
                    or row.goal_revision > goal.revision
                    or row.turn_generation != owner.turn_generation
                    or (
                        owner.active_turn.id
                        if owner.active_turn is not None
                        else owner.last_finished_turn_id
                    )
                    != row.turn_id
                ):
                    return unavailable("owner_or_goal_changed")
                reason = row.reason.value
            finally:
                conn.execute("ROLLBACK")
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return unavailable("invalid_store")
    if isinstance(goal.state, PausedGoal):
        state, explanation = goal.state.source.failure_projection()
        return FailedTurnProjection(state, explanation)
    return FailedTurnProjection("backend_suspended", reason)
