"""Goal-attempt rows and their references, independent of the store and display."""

from __future__ import annotations

import hashlib
import math
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from typing import Annotated, ClassVar, Self

from agent_comms import errors
from agent_comms.execution.goal_turn_permit import GoalTurnPermit
from agent_comms.execution.turn_failure import TurnFailure
from agent_comms.execution.turn_failure_reads import GoalFailureReservation
from agent_comms.execution.turn_item import UsageChargeId
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    AttemptId,
    AttemptToken,
    GoalId,
    MalformedRecord,
    NoAttemptId,
    TurnId,
)
from agent_comms.nominal.lifecycle_capability import Failed
from agent_comms.planning.attempt_resolution import (
    AttemptResolution,
    GoalCleared,
    NoResolution,
    WorkDecision,
)
from agent_comms.planning.goal_attempt_identity import FailureNotObserved, GoalAttemptIdentity
from agent_comms.planning.goal_attempt_phase import (
    FailedAttempt,
    GoalAttemptPhase,
    GoalAttemptTransition,
    ResolvedAttempt,
)
from agent_comms.planning.goal_failure_observation import (
    GenerationFailureSource,
    GoalFailureEvidence,
    NoFailedTurnEvidence,
)
from agent_comms.planning.goal_failure_owner import GoalFailureOwner
from agent_comms.planning.goal_generation import (
    BlockedGeneration,
    GenerationState,
    ReadyGeneration,
    ReservedGeneration,
)
from agent_comms.planning.goal_mutation import (
    GoalFailureProjection,
    GoalGenerationRead,
    GoalStoppedAttempt,
)
from agent_comms.planning.goals import Goal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import (
    ClaimedIdentity,
    RecordedThreadIncarnation,
    ThreadIncarnation,
    TurnFence,
    TurnIdentity,
)
from agent_comms.storage.typed_table import (
    LOCAL_TABLE_SOURCE,
    Column,
    DurableTables,
    ForeignKey,
    Generated,
    RowNotFoundError,
    TableReference,
    TableSource,
    TextStorage,
    TypedTable,
)


class GoalLedgerTable(DurableTables):
    """The rows of the goal-attempt database."""


class GoalLedgerWriter(ABC):
    @abstractmethod
    def _change[Result](
        self, operation: Callable[[sqlite3.Connection], Result], verify: Callable[[Result], bool],
    ) -> Result: ...

    @abstractmethod
    def _is_generation(self, generation: Generation) -> bool: ...

    @abstractmethod
    def read_attempt(self, attempt_id: AttemptId) -> LedgerAttempt: ...

    @abstractmethod
    def _connect(self) -> sqlite3.Connection: ...


class StorageUncertainError(errors.GoalAttemptError):
    """A write or durability/readback check failed; never launch from it."""


class MissingGoalGenerationError(errors.GoalAttemptRefused):
    """Historical goal has no launch record; Retry must not invent authority."""

    def __init__(self, goal_id: GoalId):
        super().__init__(
            f"Goal {goal_id} has no launch authority. Clear this goal and set it again. "
            "Retry cannot create a grant."
        )

    def diagnostic(self) -> str:
        return str(self)


@dataclass(frozen=True)
class FailedTurnObservation:
    reservation: Reservation = field(repr=False)
    evidence: FailedTurnEvidence

    @classmethod
    def from_terminal(
        cls,
        reservation: Reservation,
        *,
        owner: GoalFailureOwner,
        goal: Goal,
        lease: TurnFence,
        turn_id: TurnId,
        claim_scope: int,
        current_owner: GoalFailureOwner | None,
        current_claim: int | None,
        failure: TurnFailure,
    ) -> FailedTurnObservation | None:
        # Observation is passive: a changed owner, goal or lease yields no
        # observation. The evidence is built from already-trusted values, so a
        # construction error is a defect and raises.
        evidence = FailedTurnEvidence(
            reservation.attempt_id, reservation.goal_id, reservation.generation,
            owner.name, owner.incarnation.wire_birth, owner.worktree, claim_scope,
            lease.identity.generation, goal.revision, turn_id, type(failure), failure.text,
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


@dataclass(frozen=True)
class Generation(GoalLedgerTable, TypedTable, GoalGenerationRead, GenerationFailureSource["FailedTurnEvidence"]):
    goal_id: Annotated[GoalId, Column(primary_key=True)]
    number: Annotated[int, Column(check="number>0")]
    lifecycle: GenerationState
    attempt_id: AttemptId | NoAttemptId
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
        return GoalAttemptIdentity(self.goal_id, self.number, self.attempt_id.require_present())

    def failure_identity(self) -> GoalAttemptIdentity:
        self.lifecycle.require(Failed, FailureNotObserved())
        return self.attempt_identity

    def failure_evidence(self, conn: sqlite3.Connection) -> GoalFailureEvidence[FailedTurnEvidence]:
        identity = self.failure_identity()
        return next(FailedTurnEvidence.stream(
            conn, attempt_id=identity.attempt_id,
            goal_id=identity.goal_id, generation=identity.generation,
        ), NoFailedTurnEvidence[FailedTurnEvidence]())

    def has_ready_grant(self, number: int, digest: str) -> bool:
        return self.lifecycle.has_ready_grant(self.number == number and self.ready_digest == digest)

    def __post_init__(self) -> None:
        self.lifecycle.validate_attempt(self.attempt_id)
        if self.number < 1:
            raise ValueError("Generation must be positive.")

    @classmethod
    def _keyed(cls, db: sqlite3.Connection, key: dict[str, object], *,
               table_source: TableSource = LOCAL_TABLE_SOURCE) -> list[Self]:
        try:
            return super()._keyed(db, key, table_source=table_source)
        except (sqlite3.Error, MalformedRecord) as error:
            raise StorageUncertainError("Invalid persisted goal generation.") from error

    @classmethod
    def require_in(cls, db: sqlite3.Connection, goal_id: GoalId) -> Self:
        try:
            return cls.require(db, goal_id=goal_id)
        except RowNotFoundError as error:
            raise MissingGoalGenerationError(goal_id) from error

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
            raise errors.UnresolvedAttemptError("A valid acknowledged ready grant is required.")
        try:
            raw = bytes.fromhex(grant)
        except ValueError as error:
            raise errors.UnresolvedAttemptError("A valid acknowledged ready grant is required.") from error
        if raw.hex() != grant:
            raise errors.UnresolvedAttemptError("A valid acknowledged ready grant is required.")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class Reservation(
    GoalAttemptIdentity,
    GoalFailureReservation[GoalLedgerWriter, FailedTurnObservation],
):
    token: AttemptToken

    @property
    def identity(self) -> GoalAttemptIdentity:
        return GoalAttemptIdentity(self.goal_id, self.generation, self.attempt_id)

    def fail(
        self,
        store: GoalLedgerWriter,
        diagnostic: AttemptResolution,
        *,
        observation: FailedTurnObservation | None = None,
    ) -> Generation:
        """Block even when the model outcome is uncertain; never auto-replay."""
        reservation = self
        def write(conn: sqlite3.Connection) -> Generation:
            goal = Generation.one(conn, goal_id=reservation.goal_id)
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
            attempt.phase.record_launched_observation(conn, reservation, observation, record_observation)
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
        goal = Generation.one(conn, goal_id=self.goal_id)
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
        self, store: GoalLedgerWriter, phase: GoalAttemptPhase, generation: Generation
    ) -> bool:
        if not store._is_generation(generation):
            return False
        return store.read_attempt(self.attempt_id).holds(self, phase)


@dataclass(frozen=True)
class AttemptRecord(GoalLedgerTable, TypedTable, GoalAttemptTransition):
    reservation: Annotated[Reservation, Column(primary_key=True)]
    phase: GoalAttemptPhase
    progress_witness: str | None
    resolution: AttemptResolution

    attempt_id: ClassVar[Generated] = Generated(
        AttemptId,
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
    def read_in(cls, conn: sqlite3.Connection, attempt_id: AttemptId) -> LedgerAttempt:
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
        resolution: AttemptResolution = NoResolution(),
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
        cls, store: GoalLedgerWriter, attempt_id: AttemptId,
        phase: GoalAttemptPhase, resolution: AttemptResolution
    ) -> bool:
        return store.read_attempt(attempt_id).resolved_as(phase, resolution)


@dataclass(frozen=True)
class FailedTurnEvidence(GoalLedgerTable, TypedTable, GoalFailureEvidence["FailedTurnEvidence"]):
    attempt_id: Annotated[AttemptId, Column(primary_key=True)]
    goal_id: GoalId
    generation: Annotated[int, Column(check="generation>0")]
    owner: str
    owner_created_at: float
    worktree: Annotated[Checkout, Column(storage=TextStorage)]
    claim_generation: Annotated[
        int, Column(record_key=True, check="{claim_generation}>0")
    ]
    turn_generation: Annotated[int, Column(check="turn_generation>0")]
    goal_revision: Annotated[int, Column(check="goal_revision>=0")]
    turn_id: Annotated[TurnId, Column(unique=True)]
    failure: type[TurnFailure]
    failure_text: str = "recorded before Core kept the failure's text"
    """What the failure said; a row written before the text was kept says that instead."""

    def __post_init__(self) -> None:
        # These are the durable observation's constraints, independent of any
        # caller's authority. SQL/FieldCodec remains the single stored decoder.
        if type(self.claim_generation) is not int or self.claim_generation <= 0:
            raise ValueError("Observation claim_scope must be positive")
        if type(self.turn_generation) is not int or self.turn_generation <= 0:
            raise ValueError("Observation turn generation must be positive")
        if not math.isfinite(self.owner_created_at):
            raise ValueError("Observation incarnation must be finite")

    def execution[Execution](
        self, source: GoalFailureProjection[Execution, FailedTurnEvidence],
    ) -> Execution:
        return source.failed(self)

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

    def matches_owner(self, owner: GoalFailureOwner, claim_scope: int | None) -> bool:
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
    def references(cls) -> tuple[ForeignKey, ...]:
        return (ForeignKey(columns=("attempt_id",), target=AttemptRecord, target_columns=("attempt_id",)),)


@dataclass(frozen=True)
class LedgerAttempt(DeclaredFamily, GoalStoppedAttempt[Reservation], affix="Attempt"):
    """What the ledger holds for one attempt ID: a record, nothing, or nothing by design.

    Each member answers what the store asks of an attempt; nothing above decides it by None.
    """

    @abstractmethod
    def stopped_permit(self, make_permit: Callable[[Reservation], GoalTurnPermit]) -> GoalTurnPermit: ...

    @abstractmethod
    def reserved_by(self, reservation: Reservation, refusal: errors.GoalAttemptError) -> AttemptRecord:
        """The record this exact reservation holds; otherwise ``refusal`` is raised."""

    @abstractmethod
    def identified(self, identity: GoalAttemptIdentity, refusal: errors.GoalAttemptError) -> AttemptRecord:
        """The record with this goal, generation and attempt; otherwise ``refusal`` is raised."""

    @abstractmethod
    def holds(self, reservation: Reservation, phase: GoalAttemptPhase) -> bool: ...

    @abstractmethod
    def resolved_as(self, phase: GoalAttemptPhase, resolution: AttemptResolution) -> bool: ...

    @abstractmethod
    def retire(
        self, conn: sqlite3.Connection, lifecycle: GenerationState, goal_id: GoalId, generation: int
    ) -> None:
        """Resolve this attempt as the goal is cleared, if the generation permits it."""


@dataclass(frozen=True)
class RecordedAttempt(LedgerAttempt):
    record: AttemptRecord

    def stopped_permit(self, make_permit: Callable[[Reservation], GoalTurnPermit]) -> GoalTurnPermit:
        return make_permit(self.record.reservation)

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

    def resolved_as(self, phase: GoalAttemptPhase, resolution: AttemptResolution) -> bool:
        return self.record.phase == phase and self.record.resolution == resolution

    def retire(
        self, conn: sqlite3.Connection, lifecycle: GenerationState, goal_id: GoalId, generation: int
    ) -> None:
        attempt = self.record.reservation
        self.identified(
            GoalAttemptIdentity(goal_id, generation, attempt.attempt_id),
            errors.StaleAttemptError("Unresolved goal has no matching attempt to retire."),
        )
        if not lifecycle.permits_retirement(self.record.phase):
            raise errors.StaleAttemptError("Goal or attempt is no longer eligible for retirement.")
        self.record.advance(conn, ResolvedAttempt(), resolution=GoalCleared())


@dataclass(frozen=True)
class MissingAttempt(LedgerAttempt):
    """The ledger holds no record for an attempt ID that was named."""

    diagnostic: str = "The goal generation names no attempt."

    def stopped_permit(self, make_permit: Callable[[Reservation], GoalTurnPermit]) -> GoalTurnPermit:
        raise StorageUncertainError(self.diagnostic)

    def reserved_by(self, reservation: Reservation, refusal: errors.GoalAttemptError) -> AttemptRecord:
        raise refusal

    def identified(self, identity: GoalAttemptIdentity, refusal: errors.GoalAttemptError) -> AttemptRecord:
        raise refusal

    def holds(self, reservation: Reservation, phase: GoalAttemptPhase) -> bool:
        return False

    def resolved_as(self, phase: GoalAttemptPhase, resolution: AttemptResolution) -> bool:
        return False

    def retire(
        self, conn: sqlite3.Connection, lifecycle: GenerationState, goal_id: GoalId, generation: int
    ) -> None:
        raise errors.StaleAttemptError("Unresolved goal has no matching attempt to retire.")


@dataclass(frozen=True)
class NoAttempt(MissingAttempt):
    """The generation names no attempt, so there is none to read or resolve."""

    def retire(
        self, conn: sqlite3.Connection, lifecycle: GenerationState, goal_id: GoalId, generation: int
    ) -> None:
        lifecycle.require_retirement_without_attempt()


@dataclass(frozen=True)
class GoalWorkDecision(GoalLedgerTable, TypedTable):
    """Explicit new-work authority, named by this declaration."""
    goal_id: Annotated[
        GoalId, Column(primary_key=True, references=TableReference(Generation, ("goal_id",)))
    ]
    decision: Annotated[WorkDecision, Column(primary_key=True)]
    generation: int

    def claim(self, conn: sqlite3.Connection) -> None:
        try:
            self.insert(conn)
        except sqlite3.IntegrityError as error:
            raise errors.UnresolvedAttemptError("That work decision was already used.") from error


@dataclass(frozen=True)
class GoalProviderUsage(GoalLedgerTable, TypedTable):
    attempt_id: Annotated[
        AttemptId,
        Column(primary_key=True, references=TableReference(AttemptRecord, ("attempt_id",)))
    ]
    response_id: Annotated[UsageChargeId, Column(primary_key=True)]
    # The canonical opaque provider object remains exact; no second native usage codec.
    usage_json: str

    @classmethod
    def readback(
        cls, store: GoalLedgerWriter, attempt_id: AttemptId, response_id: UsageChargeId
    ) -> str | None:
        with closing(store._connect()) as conn:
            row = GoalProviderUsage.one(conn, attempt_id=attempt_id, response_id=response_id)
        return row.usage_json if row else None


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
