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

from .declarations import Goal, GoalPauseSource, Thread, ThreadStatus, TurnClaimFence
from .diagnostics import FailureReason
from .goal_pauses import GoalPauseEvent
from .recovery_projection import _preflight

if TYPE_CHECKING:
    from .goal_attempts import Reservation


@dataclass(frozen=True, slots=True)
class FailedTurnObservation:
    reservation: Reservation = field(repr=False)
    owner: str
    owner_created_at: float
    worktree: str
    admission: int
    turn_generation: int
    goal_revision: int
    turn_id: str
    reason: FailureReason

    @classmethod
    def from_terminal(
        cls,
        reservation: Reservation,
        *,
        owner: Thread,
        goal: Goal,
        claim: TurnClaimFence,
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
            or current_owner.pid != owner.pid
            or current_owner.goal is None
            or current_owner.goal.id != goal.id
            or current_owner.goal.revision < goal.revision
            or current_owner.turn_generation != claim.turn_generation
            or (
                current_owner.active_turn.id
                if current_owner.active_turn is not None
                else current_owner.last_finished_turn_id
            )
            != turn_id
            or current_admission != admission
            or owner.goal != goal
            or reservation.goal_id != goal.id
            or claim.name != owner.name
            or claim.created_at != owner.created_at
            or claim.turn_id != turn_id
            or claim.admission_generation != admission
            or type(admission) is not int
            or admission <= 0
            or type(claim.turn_generation) is not int
            or claim.turn_generation <= 0
            or not math.isfinite(owner.created_at)
            or not re.fullmatch(r"[0-9a-f]{32}", turn_id)
            or not isinstance(reason, FailureReason)
        ):
            return None
        return cls(
            reservation,
            owner.name,
            owner.created_at,
            owner.worktree,
            admission,
            claim.turn_generation,
            goal.revision,
            turn_id,
            reason,
        )

    def values(self) -> tuple[object, ...]:
        # Never store the reservation token or free-form diagnostic.
        return (
            self.reservation.attempt_id,
            self.reservation.goal_id,
            self.reservation.generation,
            self.owner,
            self.owner_created_at,
            self.worktree,
            self.admission,
            self.turn_generation,
            self.goal_revision,
            self.turn_id,
            self.reason.value,
        )


def create_observation_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE failed_turn_observations ("
        "attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), "
        "goal_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>0), "
        "owner TEXT NOT NULL, owner_created_at REAL NOT NULL, worktree TEXT NOT NULL, "
        "admission INTEGER NOT NULL CHECK(admission>0), "
        "turn_generation INTEGER NOT NULL CHECK(turn_generation>0), "
        "goal_revision INTEGER NOT NULL CHECK(goal_revision>=0), "
        "turn_id TEXT NOT NULL UNIQUE CHECK(length(turn_id)=32), reason TEXT NOT NULL)"
    )


def record_observation(
    conn: sqlite3.Connection, reservation: Reservation, observation: FailedTurnObservation
) -> None:
    """Optional observation cannot roll back the enclosing failure fence.

    Savepoint errors which cannot be rolled back still escape to the store's
    normal StorageUncertain path. They never yield a successful execution.
    """
    if observation.reservation != reservation:
        return
    conn.execute("SAVEPOINT passive_observation")
    try:
        conn.execute(
            "INSERT INTO failed_turn_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            observation.values(),
        )
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
        return {"schema": 1, "state": self.state, "reason": self.reason}


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
        or goal.status not in {"blocked", "paused"}
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
                if conn.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone() != ("5",):
                    return unavailable("unsupported_schema")
                row = conn.execute(
                    "SELECT o.owner,o.owner_created_at,o.worktree,o.admission,"
                    "o.goal_revision,o.reason,o.turn_id,o.turn_generation FROM goals g "
                    "JOIN attempts a ON a.attempt_id=g.attempt_id "
                    "AND a.goal_id=g.goal_id AND a.generation=g.generation "
                    "JOIN failed_turn_observations o ON o.attempt_id=a.attempt_id "
                    "AND o.goal_id=a.goal_id AND o.generation=a.generation "
                    "WHERE g.goal_id=? AND g.state='blocked' AND a.phase='failed'",
                    (goal.id,),
                ).fetchone()
                if row is None:
                    return unavailable("missing_binding")
                (
                    name,
                    created_at,
                    worktree,
                    observed_admission,
                    revision,
                    reason,
                    turn_id,
                    turn_generation,
                ) = row
                if (
                    (name, created_at, worktree, observed_admission)
                    != (owner.name, owner.created_at, owner.worktree, admission)
                    or type(revision) is not int
                    or revision > goal.revision
                    or revision < 0
                    or type(turn_generation) is not int
                    or turn_generation != owner.turn_generation
                    or (
                        owner.active_turn.id
                        if owner.active_turn is not None
                        else owner.last_finished_turn_id
                    )
                    != turn_id
                ):
                    return unavailable("owner_or_goal_changed")
                reason = FailureReason(reason).value
            finally:
                conn.execute("ROLLBACK")
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return unavailable("invalid_store")
    if goal.status == "paused":
        if (
            pause is not None
            and pause.goal_id == goal.id
            and pause.revision == goal.revision
            and pause.source is GoalPauseSource.OWNER
        ):
            return FailedTurnProjection("owner_paused", "owner_pause")
        return FailedTurnProjection("paused_uncertain", "pause_attribution_uncertain")
    return FailedTurnProjection("backend_suspended", reason)
