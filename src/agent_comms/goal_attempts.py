"""Durable reservation fence for autonomous goal attempts.

The ACP owner reserves and claims each autonomous launch before starting Pi,
then binds verified goal progress to the claimed attempt before advancing a
generation. A crash leaves reserved/claimed work unresolved; ordinary resume
cannot turn that state into another model call.

The ledger stores provider-reported usage and requires ACP to verify native
response and registry outcomes before recording success.
"""

from __future__ import annotations

from . import errors
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import tempfile
from abc import abstractmethod
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, TypeVar
from uuid import uuid4

from agent_comms.goal_turn_permit import GoalTurnPermit

from .child_process import Platform
from .declared_family import DeclaredFamily
from .field_codec import MalformedRecord
from .goal_attempt_identity import FailureNotObserved, GoalAttemptIdentity
from .goal_attempt_phase import (
    ClaimedAttempt,
    FailedAttempt,
    GoalAttemptPhase,
    ReadyAttempt,
    ReservedAttempt,
    ResolvedAttempt,
    SucceededAttempt,
)
from .goal_failure_observation import (
    FailedTurnEvidence,
    FailedTurnObservation,
    GoalFailureEvidence,
    GoalLedgerTable,
    NoFailedTurnEvidence,
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
from .lifecycle_capability import Failed
from .locked_store import DurableFile, PublishedSQLiteFile, WireWakeFile
from .pi_counts import PiCount, UnreportedCount
from .pi_payloads import PiUsage
from .store_files import AtomicFile
from .typed_table import Column, Generated, SQLiteJournalMode, TableReference, TypedTable

if TYPE_CHECKING:
    from .goal_management import Goals
    from .threads import Thread
    from .turn_context import TurnContext




class StorageUncertainError(errors.GoalAttemptError):
    """A write or durability/readback check failed; never launch from it."""


class ReservationConflictError(errors.GoalAttemptError):
    """This goal/generation is already reserved or is not ready."""


class UnresolvedAttemptError(errors.GoalAttemptError):
    """A blocked or in-flight attempt needs an explicit resolution."""

    diagnostic_text = "Goal attempt unresolved; inspect diagnostics before Retry."


class MissingGoalGenerationError(errors.GoalAttemptError, ValueError):
    """Historical goal has no launch record; Retry must not invent authority."""

    def __init__(self, goal_id: str):
        super().__init__(
            f"Goal {goal_id} has no launch authority. Clear this goal and set it again. "
            "Retry cannot create a grant."
        )

    def diagnostic(self) -> str:
        return str(self)




@dataclass(frozen=True)
class Generation(GoalLedgerTable, TypedTable):
    goal_id: Annotated[str, Column(primary_key=True)]
    number: Annotated[int, Column(check="number>0")]
    lifecycle: GenerationState
    attempt_id: str | None
    ready_digest: str = field(default="", compare=False, repr=False)
    checks = (
        f"(json_extract(lifecycle,'$.kind')='{ReadyGeneration.declared_name}' "
        "AND attempt_id IS NULL AND length(ready_digest)=64) OR "
        f"(json_extract(lifecycle,'$.kind')!='{ReadyGeneration.declared_name}' "
        "AND ready_digest='')",
        f"json_extract(lifecycle,'$.kind') IN {GenerationState.sql_choices()}",
    )

    @property
    def attempt_identity(self) -> GoalAttemptIdentity:
        assert self.attempt_id is not None
        return GoalAttemptIdentity(self.goal_id, self.number, self.attempt_id)

    def failure_identity(self) -> GoalAttemptIdentity:
        self.lifecycle.require(Failed, FailureNotObserved())
        return self.attempt_identity

    def failure_evidence(self, store: GoalAttemptStore) -> GoalFailureEvidence:
        identity = self.failure_identity()
        with closing(store._connect()) as conn:
            return next(FailedTurnEvidence.stream(
                conn, attempt_id=identity.attempt_id,
                goal_id=identity.goal_id, generation=identity.generation,
            ), NoFailedTurnEvidence())

    def has_ready_grant(self, number: int, digest: str) -> bool:
        return self.lifecycle.ready and self.number == number and self.ready_digest == digest

    def __post_init__(self) -> None:
        self.lifecycle.validate_attempt(self.attempt_id)
        if self.number < 1:
            raise ValueError("Generation must be positive.")

    @classmethod
    def read_in(cls, conn: sqlite3.Connection, goal_id: str) -> Generation | None:
        try:
            return cls.one(conn, goal_id=goal_id)
        except MalformedRecord as error:
            raise StorageUncertainError("Invalid persisted goal generation.") from error

    def advance(
        self, conn: sqlite3.Connection, successor: Generation, *, digest: str = ""
    ) -> Generation:
        if not self.lifecycle.may_become(successor.lifecycle):
            raise errors.StaleAttemptError("Generation transition is no longer permitted.")
        Generation.update(
            conn,
            where="goal_id=?",
            parameters=(self.goal_id,),
            number=successor.number,
            lifecycle=successor.lifecycle,
            attempt_id=successor.attempt_id,
            ready_digest=digest,
        )
        return successor

    @staticmethod
    def grant_digest(grant: str) -> str:
        if type(grant) is not str or len(grant) != 64:
            raise UnresolvedAttemptError("A valid acknowledged ready grant is required.")
        try:
            raw = bytes.fromhex(grant)
        except ValueError as error:
            raise UnresolvedAttemptError("A valid acknowledged ready grant is required.") from error
        if raw.hex() != grant:
            raise UnresolvedAttemptError("A valid acknowledged ready grant is required.")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class Reservation:
    goal_id: str
    generation: int
    attempt_id: str
    token: str

    @property
    def identity(self) -> GoalAttemptIdentity:
        return GoalAttemptIdentity(self.goal_id, self.generation, self.attempt_id)

    def fail(
        self,
        store: GoalAttemptStore,
        diagnostic: str,
        *,
        observation: FailedTurnObservation | None = None,
    ) -> Generation:
        """Block even when the model outcome is uncertain; never auto-replay."""
        reservation = self
        if not diagnostic.strip():
            raise ValueError("Failure diagnostic is required.")

        def write(conn: sqlite3.Connection) -> Generation:
            goal = Generation.read_in(conn, reservation.goal_id)
            late = errors.StaleAttemptError("Late failure cannot modify this goal generation.")
            attempt = AttemptRecord.read_in(conn, reservation.attempt_id).reserved_by(
                reservation, late
            )
            if goal != Generation(
                reservation.goal_id,
                reservation.generation,
                ReservedGeneration(),
                reservation.attempt_id,
            ) or not attempt.phase.may_become(FailedAttempt()):
                raise late
            attempt.advance(conn, FailedAttempt(), resolution=diagnostic)
            if observation is not None and attempt.phase.launched:
                record_observation(conn, reservation, observation)
            return goal.advance(
                conn,
                Generation(
                    reservation.goal_id,
                    reservation.generation,
                    BlockedGeneration(),
                    reservation.attempt_id,
                ),
            )

        return store._change(
            write,
            lambda value: reservation.readback(store, FailedAttempt(), value),
        )

    def require_current(self, conn: sqlite3.Connection, phase: GoalAttemptPhase) -> AttemptRecord:
        goal = Generation.read_in(conn, self.goal_id)
        stale = errors.StaleAttemptError("Attempt is no longer current or is already claimed.")
        attempt = AttemptRecord.read_in(conn, self.attempt_id).reserved_by(self, stale)
        if (
            goal
            != Generation(
                self.goal_id,
                self.generation,
                ReservedGeneration(),
                self.attempt_id,
            )
            or attempt.phase != phase
        ):
            raise stale
        return attempt

    def readback(
        self, store: GoalAttemptStore, phase: GoalAttemptPhase, generation: Generation
    ) -> bool:
        if not store._is_generation(generation):
            return False
        return store.read_attempt(self.attempt_id).holds(self, phase)


@dataclass(frozen=True)
class AttemptRecord(GoalLedgerTable, TypedTable):
    reservation: Annotated[Reservation, Column(primary_key=True)]
    phase: GoalAttemptPhase
    progress_witness: str | None
    resolution: str | None

    attempt_id: ClassVar[Generated] = Generated(
        str,
        Column(unique=True, generated="json_extract(reservation,'$.attempt_id')", nullable=True),
    )
    goal_id: ClassVar[Generated] = Generated(
        str,
        Column(
            generated="json_extract(reservation,'$.goal_id')",
            references=TableReference(Generation, ("goal_id",)),
            nullable=True,
        ),
    )
    generation: ClassVar[Generated] = Generated(
        int,
        Column(generated="json_extract(reservation,'$.generation')", nullable=True),
    )
    unique = (("goal_id", "generation"),)
    checks = (f"json_extract(phase,'$.kind') IN {GoalAttemptPhase.sql_choices()}",)

    @classmethod
    def read_in(cls, conn: sqlite3.Connection, attempt_id: str) -> LedgerAttempt:
        """The ledger's answer for one attempt ID, decoded once here: its record or its absence."""
        try:
            with closing(cls.stream(conn, attempt_id=attempt_id)) as records:
                return next(
                    (RecordedAttempt(record) for record in records),
                    MissingAttempt(f"Goal attempt {attempt_id} has no ledger row."),
                )
        except MalformedRecord as error:
            raise StorageUncertainError("Invalid persisted goal attempt.") from error

    def advance(
        self,
        conn: sqlite3.Connection,
        phase: GoalAttemptPhase,
        *,
        progress_witness: str | None = None,
        resolution: str | None = None,
    ) -> None:
        if not self.phase.may_become(phase):
            raise errors.StaleAttemptError("Attempt transition is no longer permitted.")
        AttemptRecord.update(
            conn,
            where="attempt_id=?",
            parameters=(self.reservation.attempt_id,),
            phase=phase,
            progress_witness=progress_witness,
            resolution=resolution,
        )

    @classmethod
    def matches_resolution(
        cls, store: GoalAttemptStore, attempt_id: str, phase: GoalAttemptPhase, resolution: str
    ) -> bool:
        return store.read_attempt(attempt_id).resolved_as(phase, resolution)


@dataclass(frozen=True)
class LedgerAttempt(DeclaredFamily, affix="Attempt"):
    """What the ledger holds for one attempt ID: a record, nothing, or nothing by design.

    Each member answers what the store asks of an attempt; nothing above decides it by None.
    """

    @abstractmethod
    def stopped_permit(self) -> GoalTurnPermit: ...

    @abstractmethod
    def reserved_by(self, reservation: Reservation, refusal: errors.GoalAttemptError) -> AttemptRecord:
        """The record this exact reservation holds; otherwise ``refusal`` is raised."""

    @abstractmethod
    def identified(self, identity: GoalAttemptIdentity, refusal: errors.GoalAttemptError) -> AttemptRecord:
        """The record with this goal, generation and attempt; otherwise ``refusal`` is raised."""

    @abstractmethod
    def holds(self, reservation: Reservation, phase: GoalAttemptPhase) -> bool: ...

    @abstractmethod
    def resolved_as(self, phase: GoalAttemptPhase, resolution: str) -> bool: ...

    @abstractmethod
    def retire(
        self, conn: sqlite3.Connection, lifecycle: GenerationState, goal_id: str, generation: int
    ) -> None:
        """Resolve this attempt as the goal is cleared, if the generation permits it."""


@dataclass(frozen=True)
class RecordedAttempt(LedgerAttempt):
    record: AttemptRecord

    def stopped_permit(self) -> GoalTurnPermit:
        return LaunchPermit(self.record.reservation)

    def reserved_by(self, reservation: Reservation, refusal: errors.GoalAttemptError) -> AttemptRecord:
        if self.record.reservation != reservation:
            raise refusal
        return self.record

    def identified(self, identity: GoalAttemptIdentity, refusal: errors.GoalAttemptError) -> AttemptRecord:
        if self.record.reservation.identity != identity:
            raise refusal
        return self.record

    def holds(self, reservation: Reservation, phase: GoalAttemptPhase) -> bool:
        return self.record.reservation == reservation and self.record.phase == phase

    def resolved_as(self, phase: GoalAttemptPhase, resolution: str) -> bool:
        return self.record.phase == phase and self.record.resolution == resolution

    def retire(
        self, conn: sqlite3.Connection, lifecycle: GenerationState, goal_id: str, generation: int
    ) -> None:
        attempt = self.record.reservation
        self.identified(
            GoalAttemptIdentity(goal_id, generation, attempt.attempt_id),
            errors.StaleAttemptError("Unresolved goal has no matching attempt to retire."),
        )
        if not lifecycle.permits_retirement(self.record.phase):
            raise errors.StaleAttemptError("Goal or attempt is no longer eligible for retirement.")
        self.record.advance(conn, ResolvedAttempt(), resolution="goal cleared")


@dataclass(frozen=True)
class MissingAttempt(LedgerAttempt):
    """The ledger holds no record for an attempt ID that was named."""

    diagnostic: str = "The goal generation names no attempt."

    def stopped_permit(self) -> GoalTurnPermit:
        raise StorageUncertainError(self.diagnostic)

    def reserved_by(self, reservation: Reservation, refusal: errors.GoalAttemptError) -> AttemptRecord:
        raise refusal

    def identified(self, identity: GoalAttemptIdentity, refusal: errors.GoalAttemptError) -> AttemptRecord:
        raise refusal

    def holds(self, reservation: Reservation, phase: GoalAttemptPhase) -> bool:
        return False

    def resolved_as(self, phase: GoalAttemptPhase, resolution: str) -> bool:
        return False

    def retire(
        self, conn: sqlite3.Connection, lifecycle: GenerationState, goal_id: str, generation: int
    ) -> None:
        raise errors.StaleAttemptError("Unresolved goal has no matching attempt to retire.")


@dataclass(frozen=True)
class NoAttempt(MissingAttempt):
    """The generation names no attempt, so there is none to read or resolve."""

    def retire(
        self, conn: sqlite3.Connection, lifecycle: GenerationState, goal_id: str, generation: int
    ) -> None:
        if not lifecycle.permits_retirement(None):
            raise errors.StaleAttemptError("Goal or attempt is no longer eligible for retirement.")


@dataclass(frozen=True)
class GoalWorkDecision(GoalLedgerTable, TypedTable):
    """Explicit new-work authority, named by this declaration."""
    goal_id: Annotated[str, Column(primary_key=True, references=TableReference(Generation, ("goal_id",)))]
    decision_id: Annotated[str, Column(primary_key=True)]
    generation: int

    def claim(self, conn: sqlite3.Connection) -> None:
        try:
            self.insert(conn)
        except sqlite3.IntegrityError as error:
            raise UnresolvedAttemptError("That work decision was already used.") from error


@dataclass(frozen=True)
class GoalProviderUsage(GoalLedgerTable, TypedTable):
    attempt_id: Annotated[
        str, Column(primary_key=True, references=TableReference(AttemptRecord, ("attempt_id",)))
    ]
    response_id: Annotated[str, Column(primary_key=True)]
    # The canonical opaque provider object remains exact; no second native usage codec.
    usage_json: str

    @classmethod
    def readback(cls, store: GoalAttemptStore, attempt_id: str, response_id: str) -> str | None:
        with closing(store._connect()) as conn:
            row = GoalProviderUsage.one(conn, attempt_id=attempt_id, response_id=response_id)
        return row.usage_json if row else None


def assert_goal_attempt_schema(conn: sqlite3.Connection) -> None:
    if GoalLedgerTable.schema_differs(conn):
        raise StorageUncertainError("Unsupported goal attempt schema; one-shot migration required.")


@dataclass(frozen=True, slots=True)
class LaunchPermit(GoalTurnPermit):
    reservation: Reservation

    @property
    def claims(self) -> tuple[LaunchPermit, ...]:
        return (self,)

    def has_verified_progress(self, store: GoalAttemptStore) -> bool:
        """Read the original attempt, even after its successor generation advances."""
        original = store.read_attempt(self.reservation.attempt_id)
        return original.reserved_by(self.reservation, errors.StaleAttemptError(
            "Original launch attempt changed or is unavailable.",
        )).phase.verified_progress

    def charge_for(self, goal_id: str, goals: Goals, response: str,
                   usage: PiUsage) -> bool:
        if goal_id != self.reservation.goal_id:
            return False
        self.record_provider_usage(goals.open_goal_store(), response, usage)
        return True

    def add_context(self, context: TurnContext, owner: Thread, goals: Goals) -> TurnContext:
        return context.with_goal(owner, goals)

    def fail_origin(self, store: GoalAttemptStore, goal_id: str) -> None:
        with suppress(errors.StaleAttemptError):
            self.reservation.fail(store, "Goal origin turn did not finish successfully.")

    def retire_unverified(self, store: GoalAttemptStore) -> None:
        """Release this acquired claim; settled or superseded attempts stay intact."""
        with suppress(errors.StaleAttemptError):
            self.reservation.fail(store, "Launch claim retired without verified terminal progress.")

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
            self.advance_ready(store, FailedAttempt(), "Operator stopped goal turn.")

    def advance_ready(
        self, store: GoalAttemptStore, phase: ReadyAttempt, note: str,
    ) -> Generation:
        """The ledger owns the single next-generation transaction and ready grant."""
        reservation = self.reservation

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            attempt = reservation.require_current(conn, ClaimedAttempt())
            phase.record_ready(attempt, conn, note)
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
                    None,
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
        self, store: GoalAttemptStore, response_id: str, usage: PiUsage
    ) -> None:
        """Attribute one provider response to its claimed goal attempt exactly once."""
        if not response_id:
            raise ValueError("Provider response ID is required.")
        raw = json.dumps(usage.to_wire(), sort_keys=True, separators=(",", ":"), allow_nan=False)
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
    def for_goal(cls, store: GoalAttemptStore, goal_id: str) -> ProviderUsageTotal:
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


class GoalAttemptStore(DurableFile, PublishedSQLiteFile, WireWakeFile):
    """Owner-private SQLite authority; no synthetic model or context claims.

    The caller creates a private directory and explicitly initializes it in a
    non-launching setup step. Each change uses SQLiteFile.write_transaction, SQLite's
    synchronous=EXTRA rollback journal, an explicit file fsync (plus directory
    fsync where supported), and a fresh-connection readback. Failure at any
    stage returns no permit.
    READY is only launchable with a secret issued after that readback; a visible
    post-COMMIT row whose fsync failed is never authority on its own.
    """

    filename = "goal_attempts.sqlite3"
    root_directory = "goal-private"

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.path_in(self.root)
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
            return cls(directory)
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
                    GoalLedgerTable.create_schema(conn)
            AtomicFile(staging).sync()
            # Publish only the complete schema; a concurrent initializer may win.
            # The staging descriptor is closed before its inode becomes shared.
            with suppress(FileExistsError):
                os.link(staging, path, follow_symlinks=False)
            Platform.current().sync_directory(directory)
        except (sqlite3.Error, OSError) as error:
            raise StorageUncertainError("Goal attempt initialization is uncertain.") from error
        finally:
            staging.unlink()
        return cls(directory)

    def _require_root(self) -> None:
        if not self.root.is_dir() or not Platform.current().is_private(self.root.stat(), 0o700):
            raise StorageUncertainError("Goal attempt root must remain owner-0700.")
        if (
            self.path.exists()
            and not Platform.current().is_private(self.path.stat(), 0o600)
        ):
            raise StorageUncertainError("Goal attempt database must remain owner-0600.")

    def _connect(self) -> sqlite3.Connection:
        self._require_root()
        return self.database(self.path)

    @classmethod
    def database(cls, path: Path) -> sqlite3.Connection:
        """Open this ledger's database, including a retained format at its carry boundary."""
        try:
            conn = cls.writer(path, isolation_level=None)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=EXTRA")
            modes = SQLiteJournalMode.read(conn.execute("PRAGMA journal_mode"))
            if modes != [SQLiteJournalMode("delete")]:
                conn.close()
                raise StorageUncertainError("Unexpected SQLite journal mode.")
            return conn
        except sqlite3.Error as error:
            raise StorageUncertainError("Goal attempt database unavailable.") from error

    def _sync(self) -> None:
        AtomicFile(self.path).sync()

    def _change(
        self, operation: Callable[[sqlite3.Connection], _T], verify: Callable[[_T], bool]
    ) -> _T:
        """A failed commit, sync, or readback is never a launch authorization."""
        with closing(self._connect()) as conn:
            try:
                with self.publishing_changes(conn, self.path):
                    with self.write_transaction(conn):
                        result = operation(conn)
                    self._sync()
                    try:
                        if not verify(result):
                            raise StorageUncertainError("Goal attempt readback does not attest the write.")
                    except sqlite3.Error as error:
                        raise StorageUncertainError("Goal attempt readback failed.") from error
            except errors.GoalAttemptError:
                raise
            except (sqlite3.Error, OSError) as error:
                raise StorageUncertainError(
                    "Goal attempt write durability is uncertain."
                ) from error
        return result

    def snapshot(self, goal_id: str) -> Generation | None:
        with closing(self._connect()) as conn:
            try:
                return Generation.read_in(conn, goal_id)
            except sqlite3.Error as error:
                raise StorageUncertainError("Cannot read goal attempt state.") from error

    def require_generation(self, goal_id: str) -> Generation:
        generation = self.snapshot(goal_id)
        if generation is None:
            raise MissingGoalGenerationError(goal_id)
        return generation

    def read_attempt(self, attempt_id: str) -> LedgerAttempt:
        with closing(self._connect()) as conn:
            return AttemptRecord.read_in(conn, attempt_id)

    def stopped_permit(self, goal_id: str) -> GoalTurnPermit:
        generation = self.require_generation(goal_id)
        return generation.lifecycle.stopped_permit(self, generation)

    def authorize_current_retry(self, goal_id: str, decision: str) -> None:
        """The current generation owns how an explicit retry or fresh order proceeds."""
        generation = self.require_generation(goal_id)
        generation.lifecycle.authorize_retry(self, generation, decision)

    def _is_generation(self, generation: Generation) -> bool:
        return self.snapshot(generation.goal_id) == generation

    def _ready_change(
        self, goal_id: str, generation: int, write: Callable[[sqlite3.Connection, str], Generation]
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

    def ready_grant(self, goal_id: str, generation: int) -> str:
        """Delegate only this instance's acknowledged READY authority to a supervisor."""
        refusal = UnresolvedAttemptError("No acknowledged ready grant is available; decide explicitly.")
        try:
            grant = self._ready_grants[(goal_id, generation)]
        except KeyError as error:
            raise refusal from error
        if not self.require_generation(goal_id).has_ready_grant(generation, Generation.grant_digest(grant)):
            raise refusal
        return grant

    def recover_unreserved_ready(self, goal_id: str, expected_generation: int) -> Generation:
        """Rotate an unused grant after the caller rechecks executing-owner authority.

        READY with no attempt proves this generation has never been reserved:
        reserve durably changes both fields before any launch. Rotation keeps
        the generation and invalidates older grants without replaying an attempt.
        The new secret remains in this owner instance, outside model context.
        """

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            current = Generation.read_in(conn, goal_id)
            if current != Generation(goal_id, expected_generation, ReadyGeneration(), None):
                raise ReservationConflictError(
                    "Only an unreserved READY generation can be recovered."
                )
            return current.advance(conn, current, digest=digest)

        return self._ready_change(goal_id, expected_generation, write)

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
        digest = Generation.grant_digest(ready_grant)
        attempt = Reservation(goal_id, expected_generation, uuid4().hex, uuid4().hex)

        def write(conn: sqlite3.Connection) -> Reservation:
            current = Generation.read_in(conn, goal_id)
            if current != Generation(goal_id, expected_generation, ReadyGeneration(), None):
                raise ReservationConflictError("Goal generation is not ready or already reserved.")
            if not hmac.compare_digest(current.ready_digest, digest):
                raise UnresolvedAttemptError("Ready grant is stale or was never acknowledged.")
            AttemptRecord(attempt, ReservedAttempt(), None, None).insert(conn)
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
            raise UnresolvedAttemptError("A reservation cannot be replayed after owner loss.")

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
            goal = Generation.read_in(conn, goal_id)
            changed = errors.StaleAttemptError("Attempt changed before human abandonment decision.")
            attempt = AttemptRecord.read_in(conn, attempt_id).identified(
                GoalAttemptIdentity(goal_id, expected_generation, attempt_id), changed
            )
            if goal != Generation(
                goal_id, expected_generation, ReservedGeneration(), attempt_id
            ) or not attempt.phase.may_become(FailedAttempt()):
                raise changed
            GoalWorkDecision(goal_id, user_decision_id, expected_generation).claim(conn)
            attempt.advance(conn, FailedAttempt(), resolution=user_decision_id)
            return goal.advance(
                conn,
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
                and AttemptRecord.matches_resolution(
                    self, attempt_id, FailedAttempt(), user_decision_id
                )
            ),
        )

    def resume(self, goal_id: str, expected_generation: int) -> Generation:
        """Ordinary same-ID resume never resolves a reserved or blocked attempt."""
        current = self.snapshot(goal_id)
        if current is None or current.number != expected_generation:
            raise errors.StaleAttemptError("Goal generation changed before resume.")
        if not current.lifecycle.ready or current.attempt_id is not None:
            raise UnresolvedAttemptError("Explicit attempt resolution is required before resume.")
        self.ready_grant(goal_id, expected_generation)
        return current

    def authorize_ready_recovery(
        self, goal_id: str, *, expected_generation: int, user_decision_id: str
    ) -> Generation:
        """An explicit retry or fresh order rotates lost READY authority.

        Integration must present the durable goal, verify the real retry decision
        or checked fresh order, and verify registry goal identity.
        If a reservation raced this decision, neither its grant nor its attempt
        is overwritten. Reusing a decision ID cannot mint another grant.
        """
        if not user_decision_id.strip():
            raise ValueError("An explicit user recovery decision ID is required.")

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            current = Generation.read_in(conn, goal_id)
            if current != Generation(goal_id, expected_generation, ReadyGeneration(), None):
                raise UnresolvedAttemptError(
                    "Goal is not ready; an attempt may already be unresolved."
                )
            GoalWorkDecision(goal_id, user_decision_id, expected_generation).claim(conn)
            return current.advance(
                conn,
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
        """An explicit retry or checked fresh order authorizes new work; resume alone cannot.

        This records an authorization, not a provider acknowledgement or input replay.
        Integration supplies the real retry decision or checked order's identity;
        same-UID user authentication is not claimed by the ledger.
        """
        if not user_decision_id.strip():
            raise ValueError("An explicit user retry decision ID is required.")

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            current = Generation.read_in(conn, goal_id)
            if current != Generation(goal_id, expected_generation, BlockedGeneration(), attempt_id):
                raise UnresolvedAttemptError("Blocked attempt changed or is not resolved.")
            unrecorded = UnresolvedAttemptError(
                "Only a blocked, recorded failure may be retried explicitly."
            )
            attempt = AttemptRecord.read_in(conn, attempt_id).identified(
                GoalAttemptIdentity(goal_id, expected_generation, attempt_id), unrecorded
            )
            if not isinstance(attempt.phase, FailedAttempt):
                raise unrecorded
            GoalWorkDecision(goal_id, user_decision_id, expected_generation).claim(conn)
            attempt.advance(conn, ResolvedAttempt(), resolution=user_decision_id)
            return current.advance(
                conn,
                Generation(
                    goal_id,
                    expected_generation + 1,
                    ReadyGeneration(),
                    None,
                ),
                digest=digest,
            )

        return self._ready_change(goal_id, expected_generation + 1, write)

    def retire_goal(
        self, goal_id: str, *, expected_generation: int, attempt_id: str | None
    ) -> Generation:
        """Revoke READY or an unresolved attempt before clearing the registry goal."""

        def write(conn: sqlite3.Connection) -> Generation:
            current = Generation.read_in(conn, goal_id)
            if (
                current is None
                or current.number != expected_generation
                or current.attempt_id != attempt_id
            ):
                raise errors.StaleAttemptError("Goal generation or attempt changed before retirement.")
            attempt = NoAttempt() if attempt_id is None else AttemptRecord.read_in(conn, attempt_id)
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
                    attempt_id is None
                    or AttemptRecord.matches_resolution(
                        self, attempt_id, ResolvedAttempt(), "goal cleared"
                    )
                )
            ),
        )
        self._ready_grants.pop((goal_id, expected_generation), None)
        if attempt_id is not None:
            self._owned.discard(attempt_id)
        return result
