"""Durable reservation fence for autonomous goal attempts.

The ACP owner reserves and claims each autonomous launch before starting Pi,
then binds verified goal progress to the claimed attempt before advancing a
generation. A crash leaves reserved/claimed work unresolved; ordinary resume
cannot turn that state into another model call.

The ledger stores provider-reported usage and requires ACP to verify native
response and registry outcomes before recording success.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
from collections.abc import Callable, Mapping
from contextlib import closing, suppress
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal, TypeVar
from uuid import uuid4

from .compaction_states import sql_names
from .goal_attempt_phase import (
    ClaimedAttempt,
    FailedAttempt,
    GoalAttemptPhase,
    ReservedAttempt,
    ResolvedAttempt,
    SucceededAttempt,
)
from .goal_failure_observation import (
    FailedTurnObservation,
    GoalLedgerTable,
    record_observation,
)
from .goal_generation import (
    BlockedGeneration,
    CancelledGeneration,
    CompletedGeneration,
    GenerationState,
    ReadyGeneration,
    ReservedGeneration,
)
from .typed_table import Column, SQLiteSchemaObject, TypedRow, TypedTable


class GoalAttemptError(RuntimeError):
    """A goal attempt cannot proceed as requested."""


class StorageUncertainError(GoalAttemptError):
    """A write or durability/readback check failed; never launch from it."""


class ReservationConflictError(GoalAttemptError):
    """This goal/generation is already reserved or is not ready."""


class UnresolvedAttemptError(GoalAttemptError):
    """A blocked or in-flight attempt needs an explicit resolution."""


class StaleAttemptError(GoalAttemptError):
    """A late outcome cannot modify the current goal generation."""


@dataclass(frozen=True)
class Generation(GoalLedgerTable, TypedTable):
    goal_id: str = field(metadata={"sql": Column(primary_key=True)})
    number: int = field(metadata={"sql": Column(check="number>0")})
    lifecycle: GenerationState
    attempt_id: str | None
    ready_digest: str = field(default="", compare=False, repr=False)
    checks = (
        f"(json_extract(lifecycle,'$.kind')='{ReadyGeneration.declared_name}' "
        "AND attempt_id IS NULL AND length(ready_digest)=64) OR "
        f"(json_extract(lifecycle,'$.kind')!='{ReadyGeneration.declared_name}' "
        "AND ready_digest='')",
        f"json_extract(lifecycle,'$.kind') IN {sql_names(GenerationState)}",
    )

    def __post_init__(self) -> None:
        self.lifecycle.validate_attempt(self.attempt_id)
        if self.number < 1:
            raise ValueError("Generation must be positive.")


@dataclass(frozen=True, slots=True)
class Reservation:
    goal_id: str
    generation: int
    attempt_id: str
    token: str


@dataclass(frozen=True)
class AttemptRecord(GoalLedgerTable, TypedTable):
    reservation: Reservation = field(metadata={"sql": Column(primary_key=True)})
    phase: GoalAttemptPhase
    progress_witness: str | None
    resolution: str | None

    attempt_id: str | None = field(
        default=None,
        init=False,
        compare=False,
        metadata={"sql": Column(unique=True, generated="json_extract(reservation,'$.attempt_id')")},
    )
    goal_id: str | None = field(
        default=None,
        init=False,
        compare=False,
        metadata={
            "sql": Column(
                generated="json_extract(reservation,'$.goal_id')",
                references=(Generation, "goal_id"),
            )
        },
    )
    generation: int | None = field(
        default=None,
        init=False,
        compare=False,
        metadata={"sql": Column(generated="json_extract(reservation,'$.generation')")},
    )
    unique = (("goal_id", "generation"),)
    checks = (f"json_extract(phase,'$.kind') IN {sql_names(GoalAttemptPhase)}",)


@dataclass(frozen=True)
class GoalHumanDecision(GoalLedgerTable, TypedTable):
    goal_id: str = field(
        metadata={"sql": Column(primary_key=True, references=(Generation, "goal_id"))}
    )
    decision_id: str = field(metadata={"sql": Column(primary_key=True)})
    generation: int


@dataclass(frozen=True)
class GoalProviderUsage(GoalLedgerTable, TypedTable):
    attempt_id: str = field(
        metadata={"sql": Column(primary_key=True, references=(AttemptRecord, "attempt_id"))}
    )
    response_id: str = field(metadata={"sql": Column(primary_key=True)})
    # The canonical opaque provider object remains exact; no second native usage codec.
    usage_json: str


@dataclass(frozen=True)
class GoalAttemptSchema(GoalLedgerTable, TypedTable):
    singleton: Literal[1] = field(metadata={"sql": Column(primary_key=True)})
    version: Literal[6]


@dataclass(frozen=True)
class _JournalMode(TypedRow):
    journal_mode: str


def assert_goal_attempt_schema(conn: sqlite3.Connection) -> None:
    schema = {
        name: sql
        for table in TypedTable.members_with(GoalLedgerTable)
        for name, sql in table.schema_objects().items()
    }
    actual = SQLiteSchemaObject.read(
        conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        )
    )
    if {row.name: row.sql for row in actual} != schema:
        raise StorageUncertainError("Unsupported goal attempt schema; one-shot cutover required.")
    if GoalAttemptSchema.one(conn, singleton=1) != GoalAttemptSchema(1, 6):
        raise StorageUncertainError("Unsupported goal attempt schema version.")


@dataclass(frozen=True, slots=True)
class LaunchPermit:
    reservation: Reservation


@dataclass(frozen=True, slots=True)
class ProviderUsageTotal:
    responses: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_total: Decimal = Decimal(0)


_T = TypeVar("_T")


class GoalAttemptStore:
    """Owner-private SQLite authority; no synthetic model or context claims.

    The caller creates a private directory and explicitly initializes it in a
    non-launching setup step. Each change uses BEGIN IMMEDIATE, SQLite's
    synchronous=EXTRA rollback journal, an explicit file fsync (plus directory
    fsync where supported), and a fresh-connection readback. Failure at any
    stage returns no permit.
    READY is only launchable with a secret issued after that readback; a visible
    post-COMMIT row whose fsync failed is never authority on its own.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "goal_attempts.sqlite3"
        self._require_root()
        if not self.path.is_file():
            raise StorageUncertainError("Goal attempt store is not initialized.")
        self._owned: set[str] = set()
        # These are capabilities, not a replayable mirror of SQLite state.
        # A process restart loses them and requires an explicit user decision.
        self._ready_grants: dict[tuple[str, int], str] = {}
        with closing(self._connect()) as conn:
            try:
                assert_goal_attempt_schema(conn)
            except (sqlite3.Error, ValueError, TypeError) as error:
                raise StorageUncertainError("Incomplete goal attempt schema.") from error

    @classmethod
    def initialize(cls, root: str | Path) -> GoalAttemptStore:
        """Explicit setup; creation or fsync uncertainty never returns a store."""
        directory = Path(root)
        if not directory.is_dir() or (
            os.name == "posix" and stat.S_IMODE(directory.stat().st_mode) != 0o700
        ):
            raise StorageUncertainError("An existing owner-0700 directory is required.")
        path = directory / "goal_attempts.sqlite3"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            # Existing state never recreates a process-local launch capability.
            return cls(directory)
        except OSError as error:
            raise StorageUncertainError("Could not create goal attempt store.") from error
        else:
            os.close(fd)
        try:
            with closing(sqlite3.connect(path, timeout=5, isolation_level=None)) as conn:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("PRAGMA synchronous=EXTRA")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                for table in TypedTable.members_with(GoalLedgerTable):
                    table.create(conn)
                GoalAttemptSchema(1, 6).insert(conn)
                conn.commit()
            cls._sync_paths(path, directory)
        except (sqlite3.Error, OSError) as error:
            raise StorageUncertainError("Goal attempt initialization is uncertain.") from error
        return cls(directory)

    def _require_root(self) -> None:
        if not self.root.is_dir() or (
            os.name == "posix" and stat.S_IMODE(self.root.stat().st_mode) != 0o700
        ):
            raise StorageUncertainError("Goal attempt root must remain owner-0700.")
        if (
            self.path.exists()
            and os.name == "posix"
            and stat.S_IMODE(self.path.stat().st_mode) != 0o600
        ):
            raise StorageUncertainError("Goal attempt database must remain owner-0600.")

    def _connect(self) -> sqlite3.Connection:
        self._require_root()
        try:
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=EXTRA")
            modes = _JournalMode.read(conn.execute("PRAGMA journal_mode"))
            if modes != [_JournalMode("delete")]:
                conn.close()
                raise StorageUncertainError("Unexpected SQLite journal mode.")
            return conn
        except sqlite3.Error as error:
            raise StorageUncertainError("Goal attempt database unavailable.") from error

    @staticmethod
    def _sync_paths(path: Path, directory: Path) -> None:
        # Windows' CRT _commit (used by os.fsync) needs a writable handle.
        file_fd = os.open(path, os.O_RDWR if os.name == "nt" else os.O_RDONLY)
        try:
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        if os.name == "posix":
            dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def _sync(self) -> None:
        self._sync_paths(self.path, self.root)

    @staticmethod
    def _commit(conn: sqlite3.Connection) -> None:
        conn.commit()

    def _change(
        self, operation: Callable[[sqlite3.Connection], _T], verify: Callable[[_T], bool]
    ) -> _T:
        """A failed commit, sync, or readback is never a launch authorization."""
        with closing(self._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = operation(conn)
                self._commit(conn)
                self._sync()
            except GoalAttemptError:
                if conn.in_transaction:
                    conn.rollback()
                raise
            except (sqlite3.Error, OSError) as error:
                if conn.in_transaction:
                    with suppress(sqlite3.Error):
                        conn.rollback()
                raise StorageUncertainError(
                    "Goal attempt write durability is uncertain."
                ) from error
        try:
            if not verify(result):
                raise StorageUncertainError("Goal attempt readback does not attest the write.")
        except sqlite3.Error as error:
            raise StorageUncertainError("Goal attempt readback failed.") from error
        return result

    @staticmethod
    def _generation(conn: sqlite3.Connection, goal_id: str) -> Generation | None:
        try:
            return Generation.one(conn, goal_id=goal_id)
        except (TypeError, ValueError) as error:
            raise StorageUncertainError("Invalid persisted goal generation.") from error

    @staticmethod
    def _attempt(conn: sqlite3.Connection, attempt_id: str) -> AttemptRecord | None:
        try:
            return AttemptRecord.one(conn, attempt_id=attempt_id)
        except (TypeError, ValueError) as error:
            raise StorageUncertainError("Invalid persisted goal attempt.") from error

    @staticmethod
    def _advance_attempt(
        conn: sqlite3.Connection,
        current: AttemptRecord,
        phase: GoalAttemptPhase,
        *,
        progress_witness: str | None = None,
        resolution: str | None = None,
    ) -> None:
        if not current.phase.may_become(phase):
            raise StaleAttemptError("Attempt transition is no longer permitted.")
        AttemptRecord.update(
            conn,
            where="attempt_id=?",
            parameters=(current.reservation.attempt_id,),
            phase=phase,
            progress_witness=progress_witness,
            resolution=resolution,
        )

    @staticmethod
    def _advance_generation(
        conn: sqlite3.Connection,
        current: Generation,
        successor: Generation,
        *,
        digest: str = "",
    ) -> Generation:
        if not current.lifecycle.may_become(successor.lifecycle):
            raise StaleAttemptError("Generation transition is no longer permitted.")
        Generation.update(
            conn,
            where="goal_id=?",
            parameters=(current.goal_id,),
            number=successor.number,
            lifecycle=successor.lifecycle,
            attempt_id=successor.attempt_id,
            ready_digest=digest,
        )
        return successor

    def snapshot(self, goal_id: str) -> Generation | None:
        with closing(self._connect()) as conn:
            try:
                return self._generation(conn, goal_id)
            except sqlite3.Error as error:
                raise StorageUncertainError("Cannot read goal attempt state.") from error

    def _is_generation(self, generation: Generation) -> bool:
        return self.snapshot(generation.goal_id) == generation

    @staticmethod
    def _claim_human_decision(
        conn: sqlite3.Connection, goal_id: str, generation: int, decision_id: str
    ) -> None:
        try:
            GoalHumanDecision(goal_id, decision_id, generation).insert(conn)
        except sqlite3.IntegrityError as error:
            raise UnresolvedAttemptError("That human decision was already used.") from error

    @staticmethod
    def _grant_digest(grant: str) -> str:
        if type(grant) is not str or len(grant) != 64:
            raise UnresolvedAttemptError("A valid acknowledged ready grant is required.")
        try:
            raw = bytes.fromhex(grant)
        except ValueError as error:
            raise UnresolvedAttemptError("A valid acknowledged ready grant is required.") from error
        if raw.hex() != grant:
            raise UnresolvedAttemptError("A valid acknowledged ready grant is required.")
        return hashlib.sha256(raw).hexdigest()

    def _ready_change(
        self, goal_id: str, generation: int, write: Callable[[sqlite3.Connection, str], Generation]
    ) -> Generation:
        # Generate before the transaction, but *publish* the capability only
        # after the complete commit/fsync/readback sequence has returned.
        grant = secrets.token_hex(32)
        digest = self._grant_digest(grant)
        result = self._change(
            lambda conn: write(conn, digest),
            lambda value: (
                self._is_generation(value)
                and self._read_ready_digest(value.goal_id, value.number) == digest
            ),
        )
        self._ready_grants[(goal_id, generation)] = grant
        return result

    def _read_ready_digest(self, goal_id: str, generation: int) -> str | None:
        with closing(self._connect()) as conn:
            try:
                row = Generation.one(conn, goal_id=goal_id, number=generation)
            except (sqlite3.Error, ValueError, TypeError) as error:
                raise StorageUncertainError("Cannot read ready authority state.") from error
        return row.ready_digest if row is not None and row.lifecycle.ready else None

    def ready_grant(self, goal_id: str, generation: int) -> str:
        """Delegate only this instance's acknowledged READY authority to a supervisor."""
        grant = self._ready_grants.get((goal_id, generation))
        if grant is None or self._read_ready_digest(goal_id, generation) != self._grant_digest(
            grant
        ):
            raise UnresolvedAttemptError(
                "No acknowledged ready grant is available; decide explicitly."
            )
        return grant

    def recover_unreserved_ready(self, goal_id: str, expected_generation: int) -> Generation:
        """Rotate an unused grant after the caller rechecks executing-owner authority.

        READY with no attempt proves this generation has never been reserved:
        reserve durably changes both fields before any launch. Rotation keeps
        the generation and invalidates older grants without replaying an attempt.
        The new secret remains in this owner instance, outside model context.
        """

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            current = self._generation(conn, goal_id)
            if current != Generation(goal_id, expected_generation, ReadyGeneration(), None):
                raise ReservationConflictError(
                    "Only an unreserved READY generation can be recovered."
                )
            return self._advance_generation(conn, current, current, digest=digest)

        return self._ready_change(goal_id, expected_generation, write)

    def _is_attempt(
        self, attempt: Reservation, phase: GoalAttemptPhase, generation: Generation
    ) -> bool:
        if not self._is_generation(generation):
            return False
        with closing(self._connect()) as conn:
            current = self._attempt(conn, attempt.attempt_id)
        return current is not None and current.reservation == attempt and current.phase == phase

    def create_goal(self, goal_id: str) -> Generation:
        """Register one externally created immutable goal ID, initially generation 1."""
        if not goal_id.strip():
            raise ValueError("Goal ID is required.")

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            if Generation.one(conn, goal_id=goal_id) is not None:
                raise ReservationConflictError("Goal ID already has a ledger.")
            row = Generation(goal_id, 1, ReadyGeneration(), None, ready_digest=digest)
            row.insert(conn)
            return row

        return self._ready_change(goal_id, 1, write)

    def reserve(
        self, goal_id: str, expected_generation: int, *, ready_grant: str | None = None
    ) -> Reservation:
        """Reserve once, using acknowledged authority delegated by the goal writer.

        A reopened store never reconstructs a grant from a visible SQLite row.
        It must receive the original writer's grant, or its executing owner
        must explicitly rotate a still-unreserved READY grant first.
        """
        if ready_grant is None:
            ready_grant = self._ready_grants.get((goal_id, expected_generation))
        if ready_grant is None:
            current = self.snapshot(goal_id)
            if (
                current is None
                or current.number != expected_generation
                or not current.lifecycle.ready
            ):
                raise ReservationConflictError("Goal generation is not ready or already reserved.")
            raise UnresolvedAttemptError(
                "No acknowledged ready grant; explicit recovery is required."
            )
        digest = self._grant_digest(ready_grant)
        attempt = Reservation(goal_id, expected_generation, uuid4().hex, uuid4().hex)

        def write(conn: sqlite3.Connection) -> Reservation:
            current = self._generation(conn, goal_id)
            if current != Generation(goal_id, expected_generation, ReadyGeneration(), None):
                raise ReservationConflictError("Goal generation is not ready or already reserved.")
            if not hmac.compare_digest(current.ready_digest, digest):
                raise UnresolvedAttemptError("Ready grant is stale or was never acknowledged.")
            AttemptRecord(attempt, ReservedAttempt(), None, None).insert(conn)
            self._advance_generation(
                conn,
                current,
                Generation(
                    goal_id,
                    expected_generation,
                    ReservedGeneration(),
                    attempt.attempt_id,
                ),
            )
            return attempt

        result = self._change(
            write,
            lambda value: self._is_attempt(
                value,
                ReservedAttempt(),
                Generation(goal_id, expected_generation, ReservedGeneration(), value.attempt_id),
            ),
        )
        # A recovered/new store instance cannot claim an old reservation.
        self._ready_grants.pop((goal_id, expected_generation), None)
        self._owned.add(result.attempt_id)
        return result

    def claim_launch(self, reservation: Reservation) -> LaunchPermit:
        """One-shot durable pre-launch claim; only this reserving instance may use it."""
        if reservation.attempt_id not in self._owned:
            raise UnresolvedAttemptError("A reservation cannot be replayed after owner loss.")

        def write(conn: sqlite3.Connection) -> LaunchPermit:
            current = self._require_current(conn, reservation, ReservedAttempt())
            self._advance_attempt(conn, current, ClaimedAttempt())
            return LaunchPermit(reservation)

        try:
            return self._change(
                write,
                lambda value: self._is_attempt(
                    value.reservation,
                    ClaimedAttempt(),
                    Generation(
                        reservation.goal_id,
                        reservation.generation,
                        ReservedGeneration(),
                        reservation.attempt_id,
                    ),
                ),
            )
        finally:
            # Even an uncertain claim cannot be used twice in this process.
            self._owned.discard(reservation.attempt_id)

    def record_provider_usage(
        self, permit: LaunchPermit, response_id: str, usage: Mapping[str, object]
    ) -> None:
        """Attribute one provider response to its claimed goal attempt exactly once."""
        if not response_id:
            raise ValueError("Provider response ID is required.")
        for usage_field in ("input", "output", "totalTokens"):
            value = usage.get(usage_field, 0)
            if type(value) is not int or value < 0:
                raise ValueError(f"Provider usage {usage_field} must be a nonnegative integer.")
        cost = usage.get("cost", {})
        if not isinstance(cost, Mapping):
            raise ValueError("Provider cost must be an object.")
        cost_total = cost.get("total", 0)
        if (
            type(cost_total) not in {int, float}
            or not Decimal(str(cost_total)).is_finite()
            or cost_total < 0
        ):
            raise ValueError("Provider cost total must be a nonnegative number.")
        try:
            raw = json.dumps(usage, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("Provider usage must be JSON serializable.") from error
        reservation = permit.reservation

        def write(conn: sqlite3.Connection) -> str:
            self._require_current(conn, reservation, ClaimedAttempt())
            row = GoalProviderUsage.one(
                conn, attempt_id=reservation.attempt_id, response_id=response_id
            )
            if row is not None:
                if row.usage_json != raw:
                    raise ValueError("Provider response ID has different usage.")
            else:
                GoalProviderUsage(reservation.attempt_id, response_id, raw).insert(conn)
            return raw

        self._change(
            write,
            lambda expected: (
                self._read_provider_usage(reservation.attempt_id, response_id) == expected
            ),
        )

    def _read_provider_usage(self, attempt_id: str, response_id: str) -> str | None:
        with closing(self._connect()) as conn:
            row = GoalProviderUsage.one(conn, attempt_id=attempt_id, response_id=response_id)
        return row.usage_json if row else None

    def provider_usage_total(self, goal_id: str) -> ProviderUsageTotal:
        """Sum only usage reported in provider responses attributed to this goal."""
        with closing(self._connect()) as conn:
            rows = GoalProviderUsage.select(
                conn,
                where=f"attempt_id IN (SELECT attempt_id FROM {AttemptRecord.declared_name} "
                "WHERE goal_id=?)",
                parameters=(goal_id,),
            )
        totals = ProviderUsageTotal()
        for row in rows:
            usage = json.loads(row.usage_json)
            totals = ProviderUsageTotal(
                responses=totals.responses + 1,
                input_tokens=totals.input_tokens + usage.get("input", 0),
                output_tokens=totals.output_tokens + usage.get("output", 0),
                total_tokens=totals.total_tokens + usage.get("totalTokens", 0),
                cost_total=totals.cost_total + Decimal(str(usage.get("cost", {}).get("total", 0))),
            )
        return totals

    def _require_current(
        self,
        conn: sqlite3.Connection,
        reservation: Reservation,
        phase: GoalAttemptPhase,
    ) -> AttemptRecord:
        goal = self._generation(conn, reservation.goal_id)
        attempt = self._attempt(conn, reservation.attempt_id)
        if goal != Generation(
            reservation.goal_id,
            reservation.generation,
            ReservedGeneration(),
            reservation.attempt_id,
        ) or (attempt is None or attempt.reservation != reservation or attempt.phase != phase):
            raise StaleAttemptError("Attempt is no longer current or is already claimed.")
        return attempt

    def record_failed(
        self,
        reservation: Reservation,
        diagnostic: str,
        *,
        observation: FailedTurnObservation | None = None,
    ) -> Generation:
        """Block even when the model outcome is uncertain; never auto-replay."""
        if not diagnostic.strip():
            raise ValueError("Failure diagnostic is required.")

        def write(conn: sqlite3.Connection) -> Generation:
            goal = self._generation(conn, reservation.goal_id)
            attempt = self._attempt(conn, reservation.attempt_id)
            if goal != Generation(
                reservation.goal_id,
                reservation.generation,
                ReservedGeneration(),
                reservation.attempt_id,
            ) or (
                attempt is None
                or attempt.reservation != reservation
                or not attempt.phase.may_become(FailedAttempt())
            ):
                raise StaleAttemptError("Late failure cannot modify this goal generation.")
            self._advance_attempt(conn, attempt, FailedAttempt(), resolution=diagnostic)
            if observation is not None and attempt.phase.launched:
                record_observation(conn, reservation, observation)
            return self._advance_generation(
                conn,
                goal,
                Generation(
                    reservation.goal_id,
                    reservation.generation,
                    BlockedGeneration(),
                    reservation.attempt_id,
                ),
            )

        return self._change(
            write,
            lambda value: self._is_attempt(reservation, FailedAttempt(), value),
        )

    def authorize_abandon_attempt(
        self,
        goal_id: str,
        *,
        expected_generation: int,
        attempt_id: str,
        user_decision_id: str,
    ) -> Generation:
        """A human records an orphaned reservation/claim as uncertain and blocked.

        A claimed attempt may have reached a provider: this does not cancel a
        request or authorize replay. Another explicit decision is necessary
        before `authorize_retry` can create a new READY generation.
        """
        if not user_decision_id.strip():
            raise ValueError("An explicit user abandonment decision ID is required.")

        def write(conn: sqlite3.Connection) -> Generation:
            goal = self._generation(conn, goal_id)
            attempt = self._attempt(conn, attempt_id)
            if goal != Generation(
                goal_id, expected_generation, ReservedGeneration(), attempt_id
            ) or (
                attempt is None
                or attempt.reservation.goal_id != goal_id
                or attempt.reservation.generation != expected_generation
                or not attempt.phase.may_become(FailedAttempt())
            ):
                raise StaleAttemptError("Attempt changed before human abandonment decision.")
            self._claim_human_decision(conn, goal_id, expected_generation, user_decision_id)
            self._advance_attempt(conn, attempt, FailedAttempt(), resolution=user_decision_id)
            return self._advance_generation(
                conn,
                goal,
                Generation(
                    goal_id,
                    expected_generation,
                    BlockedGeneration(),
                    attempt_id,
                ),
            )

        return self._change(
            write,
            lambda value: (
                self.snapshot(value.goal_id) == value
                and self._is_attempt_phase(attempt_id, FailedAttempt(), user_decision_id)
            ),
        )

    def _is_attempt_phase(
        self,
        attempt_id: str,
        phase: GoalAttemptPhase,
        resolution: str,
    ) -> bool:
        with closing(self._connect()) as conn:
            attempt = self._attempt(conn, attempt_id)
        return attempt is not None and attempt.phase == phase and attempt.resolution == resolution

    def resume(self, goal_id: str, expected_generation: int) -> Generation:
        """Ordinary same-ID resume never resolves a reserved or blocked attempt."""
        current = self.snapshot(goal_id)
        if current is None or current.number != expected_generation:
            raise StaleAttemptError("Goal generation changed before resume.")
        if not current.lifecycle.ready or current.attempt_id is not None:
            raise UnresolvedAttemptError("Explicit attempt resolution is required before resume.")
        self.ready_grant(goal_id, expected_generation)
        return current

    def authorize_ready_recovery(
        self, goal_id: str, *, expected_generation: int, user_decision_id: str
    ) -> Generation:
        """A human decision rotates lost READY authority after a restart/uncertain write.

        This is not an automatic retry: integration must present the durable
        goal and verify the *real* user decision and registry goal identity.
        If a reservation raced this decision, neither its grant nor its attempt
        is overwritten. Reusing a decision ID cannot mint another grant.
        """
        if not user_decision_id.strip():
            raise ValueError("An explicit user recovery decision ID is required.")

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            current = self._generation(conn, goal_id)
            if current != Generation(goal_id, expected_generation, ReadyGeneration(), None):
                raise UnresolvedAttemptError(
                    "Goal is not ready; an attempt may already be unresolved."
                )
            self._claim_human_decision(conn, goal_id, expected_generation, user_decision_id)
            return self._advance_generation(
                conn,
                current,
                Generation(
                    goal_id,
                    expected_generation + 1,
                    ReadyGeneration(),
                    None,
                ),
                digest=digest,
            )

        return self._ready_change(goal_id, expected_generation + 1, write)

    def authorize_retry(
        self,
        goal_id: str,
        *,
        expected_generation: int,
        attempt_id: str,
        user_decision_id: str,
    ) -> Generation:
        """A separate explicit user retry decision retires uncertainty; resume alone cannot.

        This records an authorization, not a provider receipt. Integration must
        route only a real user action here; same-UID user authentication is not
        claimed by the ledger.
        """
        if not user_decision_id.strip():
            raise ValueError("An explicit user retry decision ID is required.")

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            current = self._generation(conn, goal_id)
            if current != Generation(goal_id, expected_generation, BlockedGeneration(), attempt_id):
                raise UnresolvedAttemptError("Blocked attempt changed or is not resolved.")
            attempt = self._attempt(conn, attempt_id)
            if (
                attempt is None
                or attempt.reservation.goal_id != goal_id
                or attempt.reservation.generation != expected_generation
                or not isinstance(attempt.phase, FailedAttempt)
            ):
                raise UnresolvedAttemptError(
                    "Only a blocked, recorded failure may be retried explicitly."
                )
            self._claim_human_decision(conn, goal_id, expected_generation, user_decision_id)
            self._advance_attempt(conn, attempt, ResolvedAttempt(), resolution=user_decision_id)
            return self._advance_generation(
                conn,
                current,
                Generation(
                    goal_id,
                    expected_generation + 1,
                    ReadyGeneration(),
                    None,
                ),
                digest=digest,
            )

        return self._ready_change(goal_id, expected_generation + 1, write)

    def record_verified_progress(self, permit: LaunchPermit, progress_witness: str) -> Generation:
        """Advance only from a claimed launch and caller-verified progress witness.

        The integration owner verifies a successful, productive native terminal
        turn or an explicit registry progress report. A model progress report is
        optional; this primitive cannot itself verify the turn's outcome.
        """
        if not progress_witness.strip():
            raise ValueError("A nonempty verified progress witness is required.")
        reservation = permit.reservation

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            attempt = self._require_current(conn, reservation, ClaimedAttempt())
            self._advance_attempt(
                conn, attempt, SucceededAttempt(), progress_witness=progress_witness
            )
            current = Generation(
                reservation.goal_id,
                reservation.generation,
                ReservedGeneration(),
                reservation.attempt_id,
            )
            return self._advance_generation(
                conn,
                current,
                Generation(
                    reservation.goal_id,
                    reservation.generation + 1,
                    ReadyGeneration(),
                    None,
                ),
                digest=digest,
            )

        return self._ready_change(reservation.goal_id, reservation.generation + 1, write)

    def record_verified_completion(self, permit: LaunchPermit, witness: str) -> Generation:
        """Finish a claimed attempt with no successor generation or launch grant."""
        if not witness.strip():
            raise ValueError("A nonempty verified completion witness is required.")
        reservation = permit.reservation

        def write(conn: sqlite3.Connection) -> Generation:
            attempt = self._require_current(conn, reservation, ClaimedAttempt())
            self._advance_attempt(conn, attempt, SucceededAttempt(), progress_witness=witness)
            current = Generation(
                reservation.goal_id,
                reservation.generation,
                ReservedGeneration(),
                reservation.attempt_id,
            )
            return self._advance_generation(
                conn,
                current,
                Generation(
                    reservation.goal_id,
                    reservation.generation,
                    CompletedGeneration(),
                    reservation.attempt_id,
                ),
            )

        return self._change(
            write, lambda value: self._is_attempt(reservation, SucceededAttempt(), value)
        )

    def retire_goal(
        self, goal_id: str, *, expected_generation: int, attempt_id: str | None
    ) -> Generation:
        """Revoke READY or an unresolved attempt before clearing the registry goal."""

        def write(conn: sqlite3.Connection) -> Generation:
            current = self._generation(conn, goal_id)
            if (
                current is None
                or current.number != expected_generation
                or current.attempt_id != attempt_id
            ):
                raise StaleAttemptError("Goal generation or attempt changed before retirement.")
            attempt = self._attempt(conn, attempt_id) if attempt_id is not None else None
            if attempt_id is not None and (
                attempt is None
                or attempt.reservation.goal_id != goal_id
                or attempt.reservation.generation != expected_generation
            ):
                raise StaleAttemptError("Unresolved goal has no matching attempt to retire.")
            if not current.lifecycle.permits_retirement(attempt.phase if attempt else None):
                raise StaleAttemptError("Goal or attempt is no longer eligible for retirement.")
            if attempt is not None:
                self._advance_attempt(conn, attempt, ResolvedAttempt(), resolution="goal cleared")
            return self._advance_generation(
                conn,
                current,
                Generation(
                    goal_id,
                    expected_generation,
                    CancelledGeneration(),
                    attempt_id,
                ),
            )

        result = self._change(
            write,
            lambda value: (
                self._is_generation(value)
                and (
                    attempt_id is None
                    or self._is_attempt_phase(attempt_id, ResolvedAttempt(), "goal cleared")
                )
            ),
        )
        self._ready_grants.pop((goal_id, expected_generation), None)
        if attempt_id is not None:
            self._owned.discard(attempt_id)
        return result
