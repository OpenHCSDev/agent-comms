"""Identify an attempt and check whether its lifecycle permits starting it."""

from dataclasses import dataclass

from agent_comms.reason_codes import NoReasonCode
from .coordination_errors import (
    IdentityConflict,
    PublicationUncertain,
    RecoveryBlocked,
    StaleFence,
    StaleRevision,
)
from .coordination_snapshot import RecoverySnapshot
from .coordination_tables.attempts import AttemptRecord
from .owner_fence import OwnerFence, _digest
from .participant_store import ParticipantSnapshot


from .attempt_states import PromptStartingAttempt
from .coordination_tables.executions import CurrentExecutions, ExecutionRecord, PointedAttempt, PointedExecution
from .execution_states import ActiveExecution

INITIAL_LEASE_DURATION_MS = 60_000


@dataclass(frozen=True, slots=True)
class AttemptStart:
    execution_id: str
    attempt_ordinal: int
    owner_lookup: str
    owner_generation: int
    token: str
    expected_execution_revision: int
    expected_pointer_revision: int

    @property
    def digest(self) -> str:
        return _digest(self.token)

    def fence(self, revision: int) -> OwnerFence:
        return OwnerFence(
            self.execution_id,
            self.attempt_ordinal,
            self.owner_lookup,
            self.owner_generation,
            revision,
            self.token,
        )

    def replayed_fence(self, snapshot: RecoverySnapshot, old: AttemptRecord) -> OwnerFence:
        # Replay uses immutable creation identity, never mutable phase or CAS state.
        if (
            old.attempt_ordinal,
            old.owner_lookup,
            old.owner_generation,
            old.created_at_ms >= snapshot.execution.created_at_ms,
        ) != (
            self.attempt_ordinal,
            self.owner_lookup,
            self.owner_generation,
            True,
        ):
            raise IdentityConflict("prepared token is bound to a different attempt")
        return self.fence(old.revision)

    def validate(self, snapshot: RecoverySnapshot, participant: ParticipantSnapshot) -> None:
        execution = snapshot.execution
        if execution.owner_lookup != self.owner_lookup:
            raise StaleFence("attempt belongs to a different registry identity")
        ordinal = execution.lifecycle.next_attempt_ordinal
        if not participant.committed or (
            participant.owner_identity.owner_lookup,
            participant.owner_identity.generation,
        ) != (self.owner_lookup, self.owner_generation):
            raise StaleFence("owner identity or generation changed")
        if (execution.revision, participant.pointer.pointer_revision) != (
            self.expected_execution_revision,
            self.expected_pointer_revision,
        ):
            raise StaleRevision("execution or pointer revision changed")
        participant.pointer.require_idle()
        execution.lifecycle.require_attempt_start()
        if bool(snapshot.publication_intents):
            raise PublicationUncertain("no authorized bus-keyed publication resolution")
        execution.lifecycle.require_retry_start(snapshot)
        if ordinal != self.attempt_ordinal:
            raise IdentityConflict("attempt ordinal must be contiguous")
        if ordinal > execution.max_attempts:
            raise RecoveryBlocked("retry budget exhausted")
        snapshot.previous_attempt.require_newer_owner(self.owner_generation)

    def activate(self, session, snapshot: RecoverySnapshot) -> None:
        """Publish attempt, execution and current pointer in the authorized transaction."""
        db, execution = session._connection, snapshot.execution
        created = session.now(execution.updated_at_ms)
        # Versioned fixed policy: no caller-selected initial lease, no semantic mirror.
        lease = created + INITIAL_LEASE_DURATION_MS
        AttemptRecord(
            execution_id=self.execution_id,
            attempt_ordinal=self.attempt_ordinal,
            owner_lookup=execution.owner_lookup,
            owner_generation=self.owner_generation,
            owner_token_digest=self.digest,
            lifecycle=PromptStartingAttempt(lease),
            revision=1,
            last_progress_at_ms=None,
            reason_code=NoReasonCode(),
            created_at_ms=created,
            updated_at_ms=created,
        ).insert(db)
        ExecutionRecord.update(
            db,
            where="execution_id=?",
            parameters=(self.execution_id,),
            lifecycle=ActiveExecution(self.attempt_ordinal),
            revision=execution.revision + 1,
            reason_code=NoReasonCode(),
            updated_at_ms=created,
        )
        CurrentExecutions.update(
            db,
            where="owner_lookup=?",
            parameters=(execution.owner_lookup,),
            execution_id=PointedExecution(self.execution_id),
            attempt_ordinal=PointedAttempt(self.attempt_ordinal),
            pointer_revision=snapshot.pointer_revision + 1,
        )
