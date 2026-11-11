from __future__ import annotations

import hmac
import json
import os
import secrets
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, ClassVar, Self, TypeVar

from agent_comms import errors
from agent_comms.execution.child_process import Platform
from agent_comms.execution.goal_turn_permit import GoalTurnPermit
from agent_comms.execution.pi_wire import PiUsage
from agent_comms.execution.reservation_rules import ReservationRule, RuleCheck
from agent_comms.execution.turn_failure import TurnFailure
from agent_comms.execution.turn_failure_reads import GoalFailurePermit
from agent_comms.execution.turn_item import UsageChargeId
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    AttemptId,
    AttemptToken,
    FieldCodec,
    GoalId,
    MalformedRecord,
    NoAttemptId,
    TurnId,
)
from agent_comms.nominal.lifecycle_capability import Failed
from agent_comms.pi_counts import PiCount, UnreportedCount
from agent_comms.planning.attempt_resolution import (
    AttemptResolution,
    DecidedResolution,
    GoalCleared,
    NoResolution,
    OperatorStopped,
    OriginFailed,
    UnverifiedLaunchRetired,
    WorkDecision,
)
from agent_comms.planning.goal_attempt_identity import GoalAttemptIdentity
from agent_comms.planning.goal_attempt_phase import (
    ClaimedAttempt,
    FailedAttempt,
    ReadyAttempt,
    ReservedAttempt,
    ResolvedAttempt,
    SucceededAttempt,
)
from agent_comms.planning.goal_failure_owner import GoalFailureOwner
from agent_comms.planning.goal_generation import (
    BlockedGeneration,
    CancelledGeneration,
    CompletedGeneration,
    ReadyGeneration,
    ReservedGeneration,
)
from agent_comms.planning.goal_ledger import (
    AttemptRecord,
    FailedTurnEvidence,
    FailedTurnObservation,
    Generation,
    GoalLedgerTable,
    GoalLedgerWriter,
    GoalProviderUsage,
    GoalWorkDecision,
    LedgerAttempt,
    NoAttempt,
    Reservation,
    StorageUncertainError,
)
from agent_comms.planning.goal_mutation import (
    GoalContextSource,
    GoalGenerationChanges,
    GoalGenerationGrants,
)
from agent_comms.planning.goal_permit_source import GoalPermitSource, GoalStoreSource
from agent_comms.planning.goal_scope import GoalScope
from agent_comms.planning.goals import Goal
from agent_comms.registry.thread_identity import TurnFence
from agent_comms.storage.locked_store import FileStore, GoalWakeFile, SQLiteReader
from agent_comms.storage.maintenance_barrier import DrainSettledFile
from agent_comms.storage.store_files import AtomicFile
from agent_comms.storage.typed_table import ReconciledFile, SQLiteJournalMode, StoreWriter


@dataclass(frozen=True, kw_only=True)
class OrdinaryGoalGrantCheck(RuleCheck):
    permit: LaunchPermit
    store: GoalAttemptStore


class OrdinaryGoalGrantRule(ReservationRule):
    check_type = OrdinaryGoalGrantCheck
    explanation = "The captured autonomous goal launch is no longer claimed."

    def violated(self, check: OrdinaryGoalGrantCheck) -> bool:
        return not check.permit.is_claimed(check.store)


class ReservationConflictError(errors.GoalAttemptRefused):
    """This goal/generation is already reserved or is not ready."""


@dataclass(frozen=True, slots=True)
class LaunchPermit(
    GoalTurnPermit,
    GoalFailurePermit[GoalLedgerWriter, FailedTurnObservation],
):
    reservation: Reservation

    @property
    def claims(self) -> tuple[LaunchPermit, ...]:
        return (self,)

    def check_grant(self, store: GoalAttemptStore) -> None:
        OrdinaryGoalGrantCheck(permit=self, store=store).require_valid()

    def failure_observation(
        self, *, owner: GoalFailureOwner, goal: Goal, lease: TurnFence, turn_id: TurnId,
        claim_scope: int, current_owner: GoalFailureOwner | None, current_claim: int | None,
        failure: TurnFailure,
    ) -> FailedTurnObservation | None:
        return FailedTurnObservation.from_terminal(
            self.reservation, owner=owner, goal=goal, lease=lease, turn_id=turn_id,
            claim_scope=claim_scope, current_owner=current_owner, current_claim=current_claim,
            failure=failure,
        )

    def has_verified_progress(self, store: GoalAttemptStore) -> bool:
        """Read the original attempt, even after its successor generation advances."""
        original = store.read_attempt(self.reservation.attempt_id)
        return original.reserved_by(self.reservation, errors.StaleAttemptError(
            "Original launch attempt changed or is unavailable.",
        )).phase.verified_progress

    def charge_for(
        self, goal_id: GoalId, goals: GoalStoreSource[GoalAttemptStore],
        response: UsageChargeId, usage: PiUsage,
    ) -> bool:
        if goal_id != self.reservation.goal_id:
            return False
        self.record_provider_usage(goals.open_goal_store(), response, usage)
        return True

    def add_context[Owner, Goals, Context](
        self, context: GoalContextSource[Owner, Goals, Context], owner: Owner, goals: Goals,
    ) -> Context:
        return context.with_goal(owner, goals)

    def fail_origin(self, store: GoalAttemptStore, goal_id: GoalId) -> None:
        with suppress(errors.StaleAttemptError):
            self.reservation.fail(store, OriginFailed())

    def retire_unverified(self, store: GoalAttemptStore) -> None:
        """Release this acquired claim; settled or superseded attempts stay intact."""
        with suppress(errors.StaleAttemptError):
            self.reservation.fail(store, UnverifiedLaunchRetired())

    def is_claimed(self, store: GoalAttemptStore) -> bool:
        attempt = self.reservation
        return attempt.readback(
            store,
            ClaimedAttempt(),
            Generation(
                attempt.goal_id,
                attempt.generation,
                ReservedGeneration(),
                attempt.attempt_id,
            ),
        )

    def record_verified_progress(
        self, store: GoalAttemptStore, progress_witness: str
    ) -> Generation:
        """Advance only from a claimed launch and caller-verified progress witness.

        The integration owner verifies a successful, productive native terminal
        turn or an explicit registry progress report. A model progress report is
        optional; this primitive cannot itself verify the turn's outcome.
        """
        if not progress_witness.strip():
            raise ValueError("A nonempty verified progress witness is required.")
        return self.advance_ready(store, SucceededAttempt(), progress_witness)

    def stop(self, store: GoalAttemptStore) -> None:
        """Retire this attempt, not its goal or the right to make a fresh input."""
        if not self.has_verified_progress(store):
            self.advance_ready(store, FailedAttempt(), OperatorStopped())

    def advance_ready[Evidence](
        self, store: GoalAttemptStore, phase: ReadyAttempt[Evidence], evidence: Evidence,
    ) -> Generation:
        """The ledger owns the single next-generation transaction and ready grant."""
        reservation = self.reservation

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            attempt = reservation.require_current(conn, ClaimedAttempt())
            phase.record_ready(attempt, conn, evidence)
            current = Generation(
                reservation.goal_id,
                reservation.generation,
                ReservedGeneration(),
                reservation.attempt_id,
            )
            return current.advance(
                conn,
                Generation(
                    reservation.goal_id,
                    reservation.generation + 1,
                    ReadyGeneration(),
                    NoAttemptId(),
                ),
                digest=digest,
            )

        return store._ready_change(reservation.goal_id, reservation.generation + 1, write)

    def record_verified_completion(self, store: GoalAttemptStore, witness: str) -> Generation:
        """Finish a claimed attempt with no successor generation or launch grant."""
        if not witness.strip():
            raise ValueError("A nonempty verified completion witness is required.")
        reservation = self.reservation

        def write(conn: sqlite3.Connection) -> Generation:
            attempt = reservation.require_current(conn, ClaimedAttempt())
            attempt.advance(conn, SucceededAttempt(), progress_witness=witness)
            current = Generation(
                reservation.goal_id,
                reservation.generation,
                ReservedGeneration(),
                reservation.attempt_id,
            )
            return current.advance(
                conn,
                Generation(
                    reservation.goal_id,
                    reservation.generation,
                    CompletedGeneration(),
                    reservation.attempt_id,
                ),
            )

        return store._change(
            write, lambda value: reservation.readback(store, SucceededAttempt(), value)
        )

    def record_provider_usage(
        self, store: GoalAttemptStore, response_id: UsageChargeId, usage: PiUsage
    ) -> None:
        """Attribute one provider response to its claimed goal attempt exactly once."""
        raw = FieldCodec.compact(usage.to_wire(), sort_keys=True, allow_nan=False)
        reservation = self.reservation

        def write(conn: sqlite3.Connection) -> str:
            reservation.require_current(conn, ClaimedAttempt())
            row = GoalProviderUsage.one(
                conn, attempt_id=reservation.attempt_id, response_id=response_id
            )
            if row is not None:
                if row.usage_json != raw:
                    raise ValueError("Provider response ID has different usage.")
            else:
                GoalProviderUsage(reservation.attempt_id, response_id, raw).insert(conn)
            return raw

        store._change(
            write,
            lambda expected: (
                GoalProviderUsage.readback(store, reservation.attempt_id, response_id) == expected
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderUsageTotal:
    responses: int = 0
    input_tokens: PiCount = UnreportedCount()
    output_tokens: PiCount = UnreportedCount()
    total_tokens: PiCount = UnreportedCount()
    cost_total: Decimal = Decimal(0)

    @classmethod
    def for_goal(cls, store: GoalAttemptStore, goal_id: GoalId) -> ProviderUsageTotal:
        """Sum only usage reported in provider responses attributed to this goal."""
        with closing(store._connect()) as conn:
            rows = GoalProviderUsage.select(
                conn,
                where=f"attempt_id IN (SELECT attempt_id FROM {AttemptRecord.declared_name} "
                "WHERE goal_id=?)",
                parameters=(goal_id,),
            )
        totals = cls()
        for row in rows:
            usage = PiUsage.from_wire(json.loads(row.usage_json))
            totals = cls(
                responses=totals.responses + 1,
                input_tokens=totals.input_tokens.plus(usage.input),
                output_tokens=totals.output_tokens.plus(usage.output),
                total_tokens=totals.total_tokens.plus(usage.total_tokens),
                cost_total=totals.cost_total + usage.cost.charged(),
            )
        return totals


_T = TypeVar("_T")


class GoalAttemptFile(FileStore, SQLiteReader):
    """An unopened ledger can observe its counter without creating launch state."""

    def open(self) -> GoalAttemptStore:
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        return GoalAttemptStore.initialize(self.path.parent)


class GoalAttemptStore(
    GoalAttemptFile, DrainSettledFile, StoreWriter, ReconciledFile, GoalWakeFile,
    GoalPermitSource, GoalLedgerWriter, GoalGenerationChanges, GoalGenerationGrants[Reservation],
):
    """Owner-private SQLite authority; no synthetic model or context claims.

    The caller creates a private directory and explicitly initializes it in a
    non-launching setup step. Each change uses StoreWriter.transaction, SQLite's
    synchronous=EXTRA rollback journal, an explicit file fsync (plus directory
    fsync where supported), and a fresh-connection readback. Failure at any
    stage returns no permit.
    READY is only launchable with a secret issued after that readback; a visible
    post-COMMIT row whose fsync failed is never authority on its own.
    """

    filename = "goal_attempts.sqlite3"
    root_directory = "goal-private"

    @classmethod
    def table_schema(cls) -> type[GoalLedgerTable]:
        return GoalLedgerTable

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.require_path()
        if not self.path.is_file():
            raise StorageUncertainError("Goal attempt store is not initialized.")
        self._owned: set[AttemptId] = set()
        # These are capabilities, not a replayable mirror of SQLite state.
        # A process restart loses them and requires an explicit user decision.
        self._ready_grants: dict[tuple[str, int], str] = {}
        with closing(self._connect()) as conn:
            try:
                self.schema_change(self.path).opened(self.table_schema(), conn)
            except (sqlite3.Error, MalformedRecord) as error:
                raise StorageUncertainError("Incomplete goal attempt schema.") from error

    @classmethod
    def initialize(cls, root: str | Path) -> GoalAttemptStore:
        """Explicit setup; creation or fsync uncertainty never returns a store."""
        directory = Path(root)
        if not directory.is_dir() or not Platform.current().is_private(directory.stat(), 0o700):
            raise StorageUncertainError("An existing owner-0700 directory is required.")
        path = cls.path_in(directory)
        if path.exists():
            # Existing state never recreates a process-local launch capability.
            return cls(path)
        try:
            fd, name = tempfile.mkstemp(prefix=".goal-attempts-init-", dir=directory)
        except OSError as error:
            raise StorageUncertainError("Could not create goal attempt store.") from error
        staging = Path(name)
        try:
            os.close(fd)
            with closing(cls.writer(staging, isolation_level=None)) as conn:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("PRAGMA synchronous=EXTRA")
                conn.execute("PRAGMA foreign_keys=ON")
                with cls.write_transaction(conn):
                    # Creation publishes the file by link, before any store transaction.
                    cls.require_maintenance_open_at(path)
                    GoalLedgerTable.create_schema(conn)
            AtomicFile(staging).sync()
            # Publish only the complete schema; a concurrent initializer may win.
            # The staging descriptor is closed before its inode becomes shared.
            with suppress(FileExistsError):
                Platform.current().publish_new_file(staging, path)
            Platform.current().sync_directory(directory)
        except (sqlite3.Error, OSError) as error:
            raise StorageUncertainError("Goal attempt initialization is uncertain.") from error
        finally:
            staging.unlink(missing_ok=True)
        return cls(path)

    @property
    def root(self) -> Path:
        return self.path.parent

    def open(self) -> GoalAttemptStore:
        return self

    def require_path(self) -> None:
        if not self.root.is_dir() or not Platform.current().is_private(self.root.stat(), 0o700):
            raise StorageUncertainError("Goal attempt root must remain owner-0700.")
        if (
            self.path.exists()
            and not Platform.current().is_private(self.path.stat(), 0o600)
        ):
            raise StorageUncertainError("Goal attempt database must remain owner-0600.")

    def _connect(self) -> sqlite3.Connection:
        self.require_path()
        return self.database(self.path)

    @classmethod
    def database(cls, path: Path) -> sqlite3.Connection:
        """Open this ledger's database, including a retained format at its carry boundary."""
        try:
            conn = cls.writer(path, isolation_level=None)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=EXTRA")
            try:
                cls.require_database_mode(conn)
            except BaseException:
                conn.close()
                raise
            return conn
        except sqlite3.Error as error:
            raise StorageUncertainError("Goal attempt database unavailable.") from error

    @staticmethod
    def require_database_mode(conn: sqlite3.Connection) -> None:
        if SQLiteJournalMode.read(conn.execute("PRAGMA journal_mode")) != [SQLiteJournalMode("delete")]:
            raise StorageUncertainError("Unexpected SQLite journal mode.")

    @classmethod
    def begin_snapshot(cls, conn: sqlite3.Connection) -> None:
        super().begin_snapshot(conn)
        cls.require_database_mode(conn)

    def _sync(self) -> None:
        AtomicFile(self.path).sync()

    @contextmanager
    def writing(self, committed: Callable[[], None] = lambda: None) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as conn, self.transaction(conn, committed):
            yield conn

    def _change(
        self, operation: Callable[[sqlite3.Connection], _T], verify: Callable[[_T], bool]
    ) -> _T:
        """A failed commit, sync, or readback is never a launch authorization, and is never
        announced: the fsync and readback run before the change is published."""
        def attest() -> None:
            self._sync()
            try:
                if not verify(result):
                    raise StorageUncertainError("Goal attempt readback does not attest the write.")
            except sqlite3.Error as error:
                raise StorageUncertainError("Goal attempt readback failed.") from error

        try:
            with self.writing(attest) as conn:
                result = operation(conn)
        except errors.GoalAttemptError:
            raise
        except (sqlite3.Error, OSError) as error:
            raise StorageUncertainError(
                "Goal attempt write durability is uncertain."
            ) from error
        return result

    def snapshot(self, goal_id: GoalId) -> Generation | None:
        with closing(self._connect()) as conn:
            return Generation.one(conn, goal_id=goal_id)

    def require_generation(self, goal_id: GoalId) -> Generation:
        with closing(self._connect()) as conn:
            return Generation.require_in(conn, goal_id)

    def read_attempt(self, attempt_id: AttemptId) -> LedgerAttempt:
        with closing(self._connect()) as conn:
            return AttemptRecord.read_in(conn, attempt_id)

    def failures(
        self, owner_created_at: float, scope: GoalScope
    ) -> tuple[FailedTurnEvidence, ...]:
        with closing(self._connect()) as conn:
            try:
                return tuple(FailedTurnEvidence.select(
                    conn, order_by=("generation",), owner_created_at=owner_created_at,
                    **scope.failure_key(),
                ))
            except (sqlite3.Error, MalformedRecord) as error:
                raise StorageUncertainError("Cannot read goal attempt failures.") from error

    def stopped_permit(self, goal_id: GoalId) -> GoalTurnPermit:
        generation = self.require_generation(goal_id)
        return generation.lifecycle.stopped_permit(self, generation, LaunchPermit)

    def authorize_current_retry(self, goal_id: GoalId, decision: WorkDecision) -> None:
        """The current generation owns how an explicit retry or fresh order proceeds."""
        generation = self.require_generation(goal_id)
        generation.lifecycle.authorize_retry(self, generation, decision)

    def _is_generation(self, generation: Generation) -> bool:
        return self.snapshot(generation.goal_id) == generation

    def _ready_change(
        self, goal_id: GoalId, generation: int,
        write: Callable[[sqlite3.Connection, str], Generation]
    ) -> Generation:
        # Generate before the transaction, but *publish* the capability only
        # after the complete commit/fsync/readback sequence has returned.
        grant = secrets.token_hex(32)
        digest = Generation.grant_digest(grant)
        result = self._change(
            lambda conn: write(conn, digest),
            lambda value: (
                self._is_generation(value)
                and self.require_generation(value.goal_id).has_ready_grant(value.number, digest)
            ),
        )
        self._ready_grants[(goal_id, generation)] = grant
        return result

    def ready_grant(self, goal_id: GoalId, generation: int) -> str:
        """Delegate only this instance's acknowledged READY authority to a supervisor."""
        refusal = errors.UnresolvedAttemptError("No acknowledged ready grant is available; decide explicitly.")
        try:
            grant = self._ready_grants[(goal_id, generation)]
        except KeyError as error:
            raise refusal from error
        if not self.require_generation(goal_id).has_ready_grant(generation, Generation.grant_digest(grant)):
            raise refusal
        return grant

    def recover_unreserved_ready(self, goal_id: GoalId, expected_generation: int) -> Generation:
        """Rotate an unused grant after the caller rechecks executing-owner authority.

        READY with no attempt proves this generation has never been reserved:
        reserve durably changes both fields before any launch. Rotation keeps
        the generation and invalidates older grants without replaying an attempt.
        The new secret remains in this owner instance, outside model context.
        """

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            current = Generation.one(conn, goal_id=goal_id)
            if current != Generation(goal_id, expected_generation, ReadyGeneration(), NoAttemptId()):
                raise ReservationConflictError(
                    "Only an unreserved READY generation can be recovered."
                )
            return current.advance(conn, current, digest=digest)

        return self._ready_change(goal_id, expected_generation, write)

    def create_goal(self, goal_id: GoalId) -> Generation:
        """Register one externally created immutable goal ID, initially generation 1."""
        if not goal_id.strip():
            raise ValueError("Goal ID is required.")

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            if Generation.one(conn, goal_id=goal_id) is not None:
                raise ReservationConflictError("Goal ID already has a ledger.")
            row = Generation(goal_id, 1, ReadyGeneration(), NoAttemptId(), ready_digest=digest)
            row.insert(conn)
            return row

        return self._ready_change(goal_id, 1, write)

    def reserve(
        self, goal_id: GoalId, expected_generation: int, *, ready_grant: str | None = None
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
            if current is None or current.number != expected_generation:
                raise ReservationConflictError("Goal generation is not ready or already reserved.")
            current.lifecycle.require_ready()
            raise errors.UnresolvedAttemptError(
                "No acknowledged ready grant; explicit recovery is required."
            )
        digest = Generation.grant_digest(ready_grant)
        attempt = Reservation(
            goal_id, expected_generation, AttemptId.mint(), AttemptToken.mint()
        )

        def write(conn: sqlite3.Connection) -> Reservation:
            current = Generation.one(conn, goal_id=goal_id)
            if current != Generation(goal_id, expected_generation, ReadyGeneration(), NoAttemptId()):
                raise ReservationConflictError("Goal generation is not ready or already reserved.")
            if not hmac.compare_digest(current.ready_digest, digest):
                raise errors.UnresolvedAttemptError("Ready grant is stale or was never acknowledged.")
            AttemptRecord(attempt, ReservedAttempt(), None, NoResolution()).insert(conn)
            current.advance(
                conn,
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
            lambda value: value.readback(
                self,
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
            raise errors.UnresolvedAttemptError("A reservation cannot be replayed after owner loss.")

        def write(conn: sqlite3.Connection) -> LaunchPermit:
            current = reservation.require_current(conn, ReservedAttempt())
            current.advance(conn, ClaimedAttempt())
            return LaunchPermit(reservation)

        try:
            return self._change(
                write,
                lambda value: value.reservation.readback(
                    self,
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

    def resume(self, goal_id: GoalId, expected_generation: int) -> Generation:
        """Ordinary same-ID resume never resolves a reserved or blocked attempt."""
        current = self.snapshot(goal_id)
        if current is None or current.number != expected_generation:
            raise errors.StaleAttemptError("Goal generation changed before resume.")
        current.lifecycle.require_ready()
        self.ready_grant(goal_id, expected_generation)
        return current

    def authorize_ready_recovery(
        self, goal_id: GoalId, *, expected_generation: int, decision: WorkDecision
    ) -> Generation:
        """An explicit retry or fresh order rotates lost READY authority.

        Integration must present the durable goal, verify the real retry decision
        or checked fresh order, and verify registry goal identity.
        If a reservation raced this decision, neither its grant nor its attempt
        is overwritten. Reusing a decision ID cannot mint another grant.
        """
        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            current = Generation.one(conn, goal_id=goal_id)
            if current != Generation(goal_id, expected_generation, ReadyGeneration(), NoAttemptId()):
                raise errors.UnresolvedAttemptError(
                    "Goal is not ready; an attempt may already be unresolved."
                )
            GoalWorkDecision(goal_id, decision, expected_generation).claim(conn)
            return current.advance(
                conn,
                Generation(
                    goal_id,
                    expected_generation + 1,
                    ReadyGeneration(),
                    NoAttemptId(),
                ),
                digest=digest,
            )

        return self._ready_change(goal_id, expected_generation + 1, write)

    def authorize_retry(
        self,
        goal_id: GoalId,
        *,
        expected_generation: int,
        attempt_id: AttemptId,
        decision: WorkDecision,
    ) -> Generation:
        """An explicit retry or checked fresh order authorizes new work; resume alone cannot.

        This records an authorization, not a provider acknowledgement or input replay.
        Integration supplies the real retry decision or checked order's identity;
        same-UID user authentication is not claimed by the ledger.
        """
        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            current = Generation.one(conn, goal_id=goal_id)
            if current != Generation(goal_id, expected_generation, BlockedGeneration(), attempt_id):
                raise errors.UnresolvedAttemptError("Blocked attempt changed or is not resolved.")
            unrecorded = errors.UnresolvedAttemptError(
                "Only a blocked, recorded failure may be retried explicitly."
            )
            attempt = AttemptRecord.read_in(conn, attempt_id).identified(
                GoalAttemptIdentity(goal_id, expected_generation, attempt_id), unrecorded
            )
            attempt.phase.require_retryable()
            GoalWorkDecision(goal_id, decision, expected_generation).claim(conn)
            attempt.advance(conn, ResolvedAttempt(), resolution=DecidedResolution(decision))
            return current.advance(
                conn,
                Generation(
                    goal_id,
                    expected_generation + 1,
                    ReadyGeneration(),
                    NoAttemptId(),
                ),
                digest=digest,
            )

        return self._ready_change(goal_id, expected_generation + 1, write)

    def retire_goal(
        self, goal_id: GoalId, *, expected_generation: int, attempt_id: AttemptId | NoAttemptId
    ) -> Generation:
        """Revoke READY or an unresolved attempt before clearing the registry goal."""

        def write(conn: sqlite3.Connection) -> Generation:
            current = Generation.one(conn, goal_id=goal_id)
            if (
                current is None
                or current.number != expected_generation
                or current.attempt_id != attempt_id
            ):
                raise errors.StaleAttemptError("Goal generation or attempt changed before retirement.")
            attempt = next(
                (AttemptRecord.read_in(conn, identity) for identity in attempt_id.present()),
                NoAttempt(),
            )
            attempt.retire(conn, current.lifecycle, goal_id, expected_generation)
            return current.advance(
                conn,
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
                    all(
                        AttemptRecord.matches_resolution(self, identity, ResolvedAttempt(), GoalCleared())
                        for identity in attempt_id.present()
                    )
                )
            ),
        )
        self._ready_grants.pop((goal_id, expected_generation), None)
        for identity in attempt_id.present():
            self._owned.discard(identity)
        return result
