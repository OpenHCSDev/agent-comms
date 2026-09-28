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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .attempt_states import AttemptState
from .coordination import (
    COORDINATION_SCHEMA_VERSION,
    COORDINATION_SNAPSHOT_VERSION,
    ACPClientConnectivity,
    CurrentExecutions,
    ExecutionOrigin,
    ExecutionRecord,
    OwnerConnectivity,
    OwnerGenerations,
    SchemaMeta,
)
from .execution_states import ExecutionState
from .obligation_states import ResponseState
from .recovery_states import RecoveryCondition
from .typed_table import SQLiteJournalMode, SQLiteUserVersion, TypedRow

# All fields returned to a caller are enumerated below. In particular, never
# serialize FieldCodec.project(snapshot, "snapshot"): it includes a publication key.
ProjectionFailure = Literal[
    "missing", "invalid_store", "unsupported_schema", "busy", "unknown_owner", "gateway_unavailable"
]


def _nonnegative(value: int, *, minimum: int = 0) -> None:
    if value < minimum:
        raise ValueError("projection integer is below its minimum")


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    thread: str = field(metadata={"wire_required": True})

    def __post_init__(self):
        if not 1 <= len(self.thread) <= 256 or any(
            ord(character) < 32 or ord(character) == 127 for character in self.thread
        ):
            raise ValueError("invalid thread")


@dataclass(frozen=True, slots=True)
class ProjectedAttempt:
    ordinal: int
    phase: type[AttemptState]
    backend_done: bool = field(metadata={"wire_name": "backendDone"})
    # Verified exit of this attempt's Pi RPC child; NOT registry-owner death.
    backend_process_exited: bool = field(metadata={"wire_name": "backendProcessExited"})

    def __post_init__(self):
        _nonnegative(self.ordinal, minimum=1)


@dataclass(frozen=True, slots=True)
class ProjectedExecution:
    # A single owner-scoped selected execution, not an unbounded history list.
    status: type[ExecutionState]
    origin: ExecutionOrigin
    is_current: bool = field(metadata={"wire_name": "isCurrent"})
    attempt: ProjectedAttempt | None
    can_retry: bool = field(metadata={"wire_name": "canRetry"})
    publication: str | None

    def __post_init__(self):
        if self.publication is not None and self.publication not in {
            member.publication() for member in ResponseState.members_with(ResponseState)
        }:
            raise ValueError("unknown publication status")


@dataclass(frozen=True, slots=True)
class ProjectedRecovery(TypedRow):
    kind: type[RecoveryCondition]
    attempt: int
    elapsed_ms: int = field(metadata={"wire_name": "elapsedMs"})
    observed_at_ms: int = field(metadata={"wire_name": "observedAtMs"})

    def __post_init__(self):
        _nonnegative(self.attempt, minimum=1)
        _nonnegative(self.elapsed_ms)
        _nonnegative(self.observed_at_ms)


@dataclass(frozen=True, slots=True)
class ProjectedConnectivity(TypedRow):
    owner: OwnerConnectivity
    acp_client: ACPClientConnectivity = field(metadata={"wire_name": "acpClient"})
    observed_at_ms: int = field(metadata={"wire_name": "observedAtMs"})

    def __post_init__(self):
        _nonnegative(self.observed_at_ms)


@dataclass(frozen=True, slots=True)
class AvailableRecoveryProjection:
    owner: str
    sampled_at_ms: int = field(metadata={"wire_name": "sampledAtMs"})
    current: ProjectedExecution | None
    last_recovery: ProjectedRecovery | None = field(metadata={"wire_name": "lastRecovery"})
    connectivity: ProjectedConnectivity | None

    schema: Literal[1] = field(default=1, metadata={"wire_required": True, "wire_order": -2})
    availability: Literal["available"] = field(
        default="available", metadata={"wire_required": True, "wire_order": -1}
    )

    def __post_init__(self):
        _nonnegative(self.sampled_at_ms)


@dataclass(frozen=True, slots=True)
class UnavailableRecoveryProjection:
    reason: ProjectionFailure
    schema: Literal[1] = field(default=1, metadata={"wire_required": True, "wire_order": -2})
    availability: Literal["unavailable"] = field(
        default="unavailable", metadata={"wire_required": True, "wire_order": -1}
    )


RecoveryProjection = AvailableRecoveryProjection | UnavailableRecoveryProjection


@dataclass(frozen=True)
class RecoverySelection(TypedRow):
    """One joined coordinator selection, decoded once before redacted presentation."""

    execution_id: str
    origin: ExecutionOrigin
    status: type[ExecutionState]
    current_attempt_ordinal: int | None
    attempt_ordinal: int | None
    phase: type[AttemptState] | None
    backend_done: bool | None
    process_dead: bool | None
    state: type[ResponseState] | None
    receipts: Literal[0, 1]
    retry_authorized: bool

    def __post_init__(self):
        if self.current_attempt_ordinal != self.attempt_ordinal:
            raise ValueError("execution and attempt ordinals differ")
        if self.attempt_ordinal is not None:
            _nonnegative(self.attempt_ordinal, minimum=1)
            if self.phase is None or self.backend_done is None or self.process_dead is None:
                raise ValueError("selected attempt is incomplete")


def _preflight(path: Path) -> ProjectionFailure | None:
    """Never let a read-only SQLite open create a WAL shared-memory sidecar.

    WAL-mode SQLite can create -shm even when opened with mode=ro. Refuse such a
    database *before* opening SQLite; rollback-journal mode is the current
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
    versions = SQLiteUserVersion.read(connection.execute("PRAGMA user_version"))
    if versions != [SQLiteUserVersion(COORDINATION_SCHEMA_VERSION)]:
        return UnavailableRecoveryProjection("unsupported_schema")
    meta = SchemaMeta.one(connection, singleton=1)
    if meta != SchemaMeta(
        singleton=1,
        schema_version=COORDINATION_SCHEMA_VERSION,
        snapshot_version=COORDINATION_SNAPSHOT_VERSION,
    ):
        return UnavailableRecoveryProjection("unsupported_schema")

    # Canonical owner scoping remains distinct from gateway peer authentication.
    owners = OwnerGenerations.read(
        connection.execute(
            "SELECT g.* FROM owner_generations g JOIN participants p "
            "ON p.participant_lookup=g.owner_lookup "
            "WHERE g.owner_lookup=? AND g.owner_thread=? AND p.committed=1",
            (owner_lookup, owner_thread),
        )
    )
    if not owners:
        return UnavailableRecoveryProjection("unknown_owner")
    pointer = CurrentExecutions.one(connection, owner_lookup=owner_lookup)
    if pointer is None:
        return UnavailableRecoveryProjection("invalid_store")
    active = ExecutionRecord.read(
        connection.execute(
            "SELECT * FROM executions WHERE owner_lookup=? AND status='active' LIMIT 2",
            (owner_lookup,),
        )
    )
    if (pointer.execution_id is None and active) or (
        pointer.execution_id is not None
        and (
            len(active) != 1
            or (active[0].execution_id, active[0].current_attempt_ordinal)
            != (pointer.execution_id, pointer.attempt_ordinal)
        )
    ):
        return UnavailableRecoveryProjection("invalid_store")

    # A current pointer wins. Otherwise display precisely the latest execution
    # with a deterministic tie-break; no unbounded history or cross-owner rows.
    rows = RecoverySelection.read(
        connection.execute(
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
            (owner_lookup, pointer.execution_id),
        )
    )
    selected = next(iter(rows), None)
    projected: ProjectedExecution | None = None
    last_recovery: ProjectedRecovery | None = None
    connectivity: ProjectedConnectivity | None = None
    if selected is not None:
        execution_id = selected.execution_id
        origin, status = selected.origin, selected.status
        is_current = (
            execution_id == pointer.execution_id
            and selected.current_attempt_ordinal == pointer.attempt_ordinal
        )
        if (pointer.execution_id is not None and not is_current) or (
            is_current and not status.active
        ):
            return UnavailableRecoveryProjection("invalid_store")
        attempt = (
            ProjectedAttempt(
                selected.attempt_ordinal,
                selected.phase,
                selected.backend_done,
                selected.process_dead,
            )
            if selected.attempt_ordinal is not None
            else None
        )
        publication: str | None
        if origin is ExecutionOrigin.WIRE:
            if selected.state is None or selected.state.published != (selected.receipts == 1):
                return UnavailableRecoveryProjection("invalid_store")
            publication = selected.state.publication()
        else:
            if selected.state is not None or selected.receipts != 0:
                return UnavailableRecoveryProjection("invalid_store")
            publication = None
        projected = ProjectedExecution(
            status,
            origin,
            is_current,
            attempt,
            selected.retry_authorized
            and status.retry
            and attempt is not None
            and attempt.phase.failed
            and attempt.backend_done
            and attempt.backend_process_exited
            and not is_current,
            publication,
        )
        audits = ProjectedRecovery.read(
            connection.execute(
                "SELECT kind, attempt, elapsed_ms, observed_at_ms "
                "FROM recovery_audit WHERE execution_id=? ORDER BY audit_id DESC LIMIT 1",
                (execution_id,),
            )
        )
        last_recovery = next(iter(audits), None)
        facets = ProjectedConnectivity.read(
            connection.execute(
                "SELECT owner, acp_client, observed_at_ms "
                "FROM connectivity WHERE execution_id=?",
                (execution_id,),
            )
        )
        connectivity = next(iter(facets), None)
    elif pointer.execution_id is not None:
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
            if SQLiteJournalMode.read(connection.execute("PRAGMA journal_mode")) != [
                SQLiteJournalMode("delete")
            ]:
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
