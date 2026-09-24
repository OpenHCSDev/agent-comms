"""Read-only, snapshots-only recovery presentation from the private coordinator.

This is a *data reader*, not an authentication boundary or an ACP endpoint. The
caller must supply a trusted coordinator path and a registered canonical owner
identity; a future gateway must authenticate the requesting viewer separately.
No revision, delta, compaction lifecycle, or publication receipt is projected.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .coordination import (
    COORDINATION_SCHEMA_VERSION,
    COORDINATION_SNAPSHOT_VERSION,
    ACPClientConnectivity,
    AttemptPhase,
    ExecutionOrigin,
    ExecutionStatus,
    ObligationState,
    OwnerConnectivity,
    RecoveryKind,
)

# All fields returned to a caller are enumerated below. In particular, never
# serialize RecoverySnapshot.to_primitive(): it includes a publication key.
ProjectionFailure = Literal[
    "missing", "invalid_store", "unsupported_schema", "busy", "unknown_owner"
]
PublicationStatus = Literal["pending", "uncertain", "deferred", "published", "silent", "failed"]
_PUBLICATION_STATES: dict[ObligationState, PublicationStatus] = {
    ObligationState.PENDING: "pending",
    ObligationState.PUBLISHING: "uncertain",
    ObligationState.DEFERRED: "deferred",
    ObligationState.PUBLISHED: "published",
    ObligationState.SILENT: "silent",
    ObligationState.FAILED: "failed",
}


@dataclass(frozen=True, slots=True)
class ProjectedAttempt:
    ordinal: int
    phase: AttemptPhase
    backend_done: bool
    # Verified exit of this attempt's Pi RPC child; NOT registry-owner death.
    backend_process_exited: bool

    def to_primitive(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "phase": self.phase.value,
            "backendDone": self.backend_done,
            "backendProcessExited": self.backend_process_exited,
        }


@dataclass(frozen=True, slots=True)
class ProjectedExecution:
    # A single owner-scoped selected execution, not an unbounded history list.
    status: ExecutionStatus
    origin: ExecutionOrigin
    is_current: bool
    attempt: ProjectedAttempt | None
    can_retry: bool
    publication: PublicationStatus | None

    def to_primitive(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "origin": self.origin.value,
            "isCurrent": self.is_current,
            "attempt": self.attempt.to_primitive() if self.attempt else None,
            "canRetry": self.can_retry,
            "publication": self.publication,
        }


@dataclass(frozen=True, slots=True)
class ProjectedRecovery:
    kind: RecoveryKind
    attempt: int
    elapsed_ms: int
    observed_at_ms: int

    def to_primitive(self) -> dict[str, object]:
        # The database only bounds reason/detail, not their contents. Neither
        # can cross this privacy boundary, even if labeled "sanitized".
        return {
            "kind": self.kind.value,
            "attempt": self.attempt,
            "elapsedMs": self.elapsed_ms,
            "observedAtMs": self.observed_at_ms,
        }


@dataclass(frozen=True, slots=True)
class ProjectedConnectivity:
    owner: OwnerConnectivity
    acp_client: ACPClientConnectivity
    observed_at_ms: int

    def to_primitive(self) -> dict[str, object]:
        return {
            "owner": self.owner.value,
            "acpClient": self.acp_client.value,
            "observedAtMs": self.observed_at_ms,
        }


@dataclass(frozen=True, slots=True)
class AvailableRecoveryProjection:
    owner: str
    sampled_at_ms: int
    current: ProjectedExecution | None
    last_recovery: ProjectedRecovery | None
    connectivity: ProjectedConnectivity | None

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema": 1,
            "availability": "available",
            "owner": self.owner,
            # Sampled time is neither a transaction revision nor a phase start.
            "sampledAtMs": self.sampled_at_ms,
            "current": self.current.to_primitive() if self.current else None,
            "lastRecovery": self.last_recovery.to_primitive() if self.last_recovery else None,
            "connectivity": self.connectivity.to_primitive() if self.connectivity else None,
        }


@dataclass(frozen=True, slots=True)
class UnavailableRecoveryProjection:
    reason: ProjectionFailure

    def to_primitive(self) -> dict[str, object]:
        return {"schema": 1, "availability": "unavailable", "reason": self.reason}


RecoveryProjection = AvailableRecoveryProjection | UnavailableRecoveryProjection


def _sqlite_integer(value: object, *, minimum: int, maximum: int | None = None) -> bool:
    """SQLite facts must have their exact stored integer domain, never truthiness."""
    return (
        isinstance(value, int)
        and type(value) is int
        and value >= minimum
        and (maximum is None or value <= maximum)
    )


def _preflight(path: Path) -> ProjectionFailure | None:
    """Never let a read-only SQLite open create a WAL shared-memory sidecar.

    WAL-mode SQLite can create -shm even when opened with mode=ro. Refuse such a
    database *before* opening SQLite; rollback-journal mode is the frozen v2
    coordinator configuration. Do not create directories, chmod, or recover a
    hot journal. A concurrent WAL conversion is outside this schema contract.
    """
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return "invalid_store"
        with path.open("rb") as stream:
            header = stream.read(100)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "invalid_store"
    if len(header) < 100 or header[:16] != b"SQLite format 3\0":
        return "invalid_store"
    if header[18:20] != b"\x01\x01":
        return "invalid_store"
    return None


def _read_in_transaction(
    connection: sqlite3.Connection, owner_lookup: str, owner_thread: str
) -> RecoveryProjection:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != COORDINATION_SCHEMA_VERSION:
        return UnavailableRecoveryProjection("unsupported_schema")
    meta = connection.execute(
        "SELECT schema_version, snapshot_version FROM schema_meta WHERE singleton = 1"
    ).fetchone()
    if meta is None or tuple(meta) != (COORDINATION_SCHEMA_VERSION, COORDINATION_SNAPSHOT_VERSION):
        return UnavailableRecoveryProjection("unsupported_schema")

    # Owner-name validation is *not* authentication. An authenticated gateway
    # must bind this lookup/thread tuple to its viewer before calling us.
    owner = connection.execute(
        "SELECT 1 FROM owner_generations g JOIN participants p "
        "ON p.participant_lookup = g.owner_lookup "
        "WHERE g.owner_lookup = ? AND g.owner_thread = ? AND p.committed = 1",
        (owner_lookup, owner_thread),
    ).fetchone()
    if owner is None:
        return UnavailableRecoveryProjection("unknown_owner")
    pointer = connection.execute(
        "SELECT execution_id, attempt_ordinal FROM current_executions WHERE owner_lookup = ?",
        (owner_lookup,),
    ).fetchone()
    if pointer is None:
        return UnavailableRecoveryProjection("invalid_store")
    if (pointer[0] is None) != (pointer[1] is None) or (
        pointer[1] is not None and not _sqlite_integer(pointer[1], minimum=1)
    ):
        return UnavailableRecoveryProjection("invalid_store")
    # The pointer and all ACTIVE executions must agree. A corrupt store with a
    # cleared pointer cannot present an ACTIVE row as noncurrent or apparently
    # idle. The (owner_lookup, status) index bounds this existence check.
    active = connection.execute(
        "SELECT execution_id, current_attempt_ordinal FROM executions "
        "INDEXED BY execution_owner_status_idx "
        "WHERE owner_lookup = ? AND status = 'active' LIMIT 2",
        (owner_lookup,),
    ).fetchall()
    if (pointer[0] is None and active) or (
        pointer[0] is not None
        and (len(active) != 1 or tuple(active[0]) != (pointer[0], pointer[1]))
    ):
        return UnavailableRecoveryProjection("invalid_store")

    # A current pointer wins. Otherwise display precisely the latest execution
    # with a deterministic tie-break; no unbounded history or cross-owner rows.
    selected = connection.execute(
        "SELECT e.execution_id, e.origin, e.status, e.current_attempt_ordinal, "
        "a.attempt_ordinal, a.phase, a.backend_done, a.process_dead, "
        "o.state, (SELECT count(*) FROM publication_receipts r "
        "WHERE r.execution_id = e.execution_id) AS receipts, "
        "(SELECT authorized FROM retry_disposition_basis b "
        "WHERE b.execution_id = e.execution_id) AS retry_authorized "
        "FROM executions e "
        "LEFT JOIN attempts a ON a.execution_id = e.execution_id "
        "AND a.attempt_ordinal = e.current_attempt_ordinal AND a.owner_lookup = e.owner_lookup "
        "LEFT JOIN obligations o ON o.execution_id = e.execution_id "
        "WHERE e.owner_lookup = ? "
        "ORDER BY (e.execution_id = ?) DESC, e.updated_at_ms DESC, e.execution_id ASC LIMIT 1",
        (owner_lookup, pointer[0]),
    ).fetchone()
    projected: ProjectedExecution | None = None
    last_recovery: ProjectedRecovery | None = None
    connectivity: ProjectedConnectivity | None = None
    if selected is not None:
        (
            execution_id,
            origin_text,
            status_text,
            ordinal,
            attempt_ordinal,
            phase,
            done,
            dead,
            obligation,
            receipts,
            retry,
        ) = selected
        origin = ExecutionOrigin(origin_text)
        status = ExecutionStatus(status_text)
        if not _sqlite_integer(receipts, minimum=0, maximum=1) or not _sqlite_integer(
            retry, minimum=0, maximum=1
        ):
            return UnavailableRecoveryProjection("invalid_store")
        if (ordinal is None) != (attempt_ordinal is None) or ordinal != attempt_ordinal:
            return UnavailableRecoveryProjection("invalid_store")
        if ordinal is not None and not _sqlite_integer(ordinal, minimum=1):
            return UnavailableRecoveryProjection("invalid_store")
        if attempt_ordinal is not None and not _sqlite_integer(attempt_ordinal, minimum=1):
            return UnavailableRecoveryProjection("invalid_store")
        is_current = execution_id == pointer[0] and ordinal == pointer[1]
        if (pointer[0] is not None and not is_current) or (
            is_current and status is not ExecutionStatus.ACTIVE
        ):
            return UnavailableRecoveryProjection("invalid_store")
        if attempt_ordinal is not None:
            if not _sqlite_integer(done, minimum=0, maximum=1) or not _sqlite_integer(
                dead, minimum=0, maximum=1
            ):
                return UnavailableRecoveryProjection("invalid_store")
            attempt = ProjectedAttempt(attempt_ordinal, AttemptPhase(phase), done == 1, dead == 1)
        else:
            attempt = None
        publication: PublicationStatus | None
        if origin is ExecutionOrigin.WIRE:
            if obligation is None or (obligation == "published") != (receipts == 1):
                return UnavailableRecoveryProjection("invalid_store")
            state = ObligationState(obligation)
            publication = _PUBLICATION_STATES[state]
        else:
            if obligation is not None or receipts != 0:
                return UnavailableRecoveryProjection("invalid_store")
            publication = None
        projected = ProjectedExecution(
            status,
            origin,
            is_current,
            attempt,
            retry == 1
            and status is ExecutionStatus.DEFERRED
            and attempt is not None
            and attempt.phase is AttemptPhase.ATTEMPT_FAILED
            and attempt.backend_done
            and attempt.backend_process_exited
            and not is_current,
            publication,
        )
        audit = connection.execute(
            "SELECT kind, attempt, elapsed_ms, observed_at_ms "
            "FROM recovery_audit WHERE execution_id = ? "
            "ORDER BY audit_id DESC LIMIT 1",
            (execution_id,),
        ).fetchone()
        if audit is not None:
            if not (
                _sqlite_integer(audit[1], minimum=1)
                and _sqlite_integer(audit[2], minimum=0)
                and _sqlite_integer(audit[3], minimum=0)
            ):
                return UnavailableRecoveryProjection("invalid_store")
            last_recovery = ProjectedRecovery(RecoveryKind(audit[0]), audit[1], audit[2], audit[3])
        facet = connection.execute(
            "SELECT owner_state, acp_client_state, observed_at_ms FROM connectivity "
            "WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        if facet is not None:
            if not _sqlite_integer(facet[2], minimum=0):
                return UnavailableRecoveryProjection("invalid_store")
            connectivity = ProjectedConnectivity(
                OwnerConnectivity(facet[0]), ACPClientConnectivity(facet[1]), facet[2]
            )
    elif pointer[0] is not None:
        return UnavailableRecoveryProjection("invalid_store")
    return AvailableRecoveryProjection(
        owner_thread, int(time.time() * 1000), projected, last_recovery, connectivity
    )


def read_recovery_projection(
    path: str | os.PathLike[str], *, owner_lookup: str, owner_thread: str
) -> RecoveryProjection:
    """Read one committed owner-scoped SQLite view without starting an owner.

    Caller supplies a trusted private path and canonical registered owner. This
    function does *not* authorize an end user. No state change, file creation,
    owner process, live event, or global commit ordering is implied.
    """
    database = Path(path)
    if failure := _preflight(database):
        return UnavailableRecoveryProjection(failure)
    try:
        # mode=ro, NOT immutable=1: SQLite must respect concurrent commits.
        # Refuse WAL before this open, so it cannot create a -shm sidecar.
        with closing(
            sqlite3.connect(
                database.resolve().as_uri() + "?mode=ro",
                uri=True,
                isolation_level=None,
                timeout=0.25,
            )
        ) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 250")
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                return UnavailableRecoveryProjection("invalid_store")
            connection.execute("BEGIN")
            try:
                result = _read_in_transaction(connection, owner_lookup, owner_thread)
            finally:
                connection.execute("ROLLBACK")
            return result
    except sqlite3.OperationalError as error:
        if "locked" in str(error).lower() or "busy" in str(error).lower():
            return UnavailableRecoveryProjection("busy")
        return UnavailableRecoveryProjection("invalid_store")
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError):
        return UnavailableRecoveryProjection("invalid_store")
