"""Attempt admission identity and the cross-lifecycle rule permitting its start."""

from dataclasses import dataclass

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
from .coordination_tables.executions import ExecutionRecord, CurrentExecutions
from .execution_states import ActiveExecution

INITIAL_LEASE_DURATION_MS = 60_000


@dataclass(frozen=True, slots=True)
class AttemptStart:
    execution_id: str
    attempt_ordinal: int
    owner_thread: str
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
            self.owner_thread,
            self.owner_generation,
            revision,
            self.token,
        )

    def replayed_fence(self, snapshot: RecoverySnapshot, old: AttemptRecord) -> OwnerFence:
        # Replay uses immutable creation identity, never mutable phase or CAS state.
        if (
            old.attempt_ordinal,
            old.owner_lookup,
            old.owner_thread,
            old.owner_generation,
            old.created_at_ms >= snapshot.execution.created_at_ms,
        ) != (
            self.attempt_ordinal,
            snapshot.execution.owner_lookup,
            self.owner_thread,
            self.owner_generation,
            True,
        ):
            raise IdentityConflict("prepared token is bound to a different attempt")
        return self.fence(old.revision)

    def validate(self, snapshot: RecoverySnapshot, participant: ParticipantSnapshot) -> None:
        execution = snapshot.execution
        ordinal = (execution.lifecycle.current_attempt_ordinal or 0) + 1
        if not participant.committed or (
            participant.owner_thread,
            participant.participant_generation,
        ) != (self.owner_thread, self.owner_generation):
            raise StaleFence("owner generation or thread changed")
        if (execution.revision, participant.pointer.pointer_revision) != (
            self.expected_execution_revision,
            self.expected_pointer_revision,
        ):
            raise StaleRevision("execution or pointer revision changed")
        if participant.pointer.execution_id is not None:
            raise StaleFence("owner has a current attempt")
        if not execution.lifecycle.starts_attempt:
            raise IdentityConflict("execution cannot start an attempt")
        if snapshot.publication_intent is not None:
            raise PublicationUncertain("no authorized bus-keyed publication resolution")
        if execution.lifecycle.retry and snapshot.attempt is None:
            raise RecoveryBlocked("frozen v2 cannot resume an unstarted deferral")
        if execution.lifecycle.retry and not snapshot.can_retry:
            raise RecoveryBlocked("retry requires final death, replay proof, and budget")
        if ordinal != self.attempt_ordinal:
            raise IdentityConflict("attempt ordinal must be contiguous")
        if ordinal > execution.max_attempts:
            raise RecoveryBlocked("retry budget exhausted")
        if (
            snapshot.attempt is not None
            and self.owner_generation <= snapshot.attempt.owner_generation
        ):
            raise StaleFence("retry must use a strictly newer owner generation")

    def activate(self, session, snapshot: RecoverySnapshot) -> None:
        """Publish attempt, execution and current pointer in the admitted transaction."""
        db, execution = session._connection, snapshot.execution
        created = session.now(execution.updated_at_ms)
        # Versioned fixed policy: no caller-selected initial lease, no semantic mirror.
        lease = created + INITIAL_LEASE_DURATION_MS
        AttemptRecord(
            execution_id=self.execution_id,
            attempt_ordinal=self.attempt_ordinal,
            owner_lookup=execution.owner_lookup,
            owner_thread=self.owner_thread,
            owner_generation=self.owner_generation,
            owner_token_digest=self.digest,
            lifecycle=PromptStartingAttempt(lease),
            revision=1,
            last_progress_at_ms=None,
            reason_code=None,
            created_at_ms=created,
            updated_at_ms=created,
        ).insert(db)
        ExecutionRecord.update(
            db,
            where="execution_id=?",
            parameters=(self.execution_id,),
            lifecycle=ActiveExecution(self.attempt_ordinal),
            revision=execution.revision + 1,
            reason_code=None,
            updated_at_ms=created,
        )
        CurrentExecutions.update(
            db,
            where="owner_lookup=?",
            parameters=(execution.owner_lookup,),
            execution_id=self.execution_id,
            attempt_ordinal=self.attempt_ordinal,
            pointer_revision=snapshot.pointer_revision + 1,
        )
