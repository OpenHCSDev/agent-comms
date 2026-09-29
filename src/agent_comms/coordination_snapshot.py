"""Cross-record recovery consistency and retry authorization."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

from agent_comms.coordination_errors import (
    IntegrityViolationError,
    StaleFence,
    RecoveryBlocked,
    PublicationUncertain,
    IdentityConflict,
)
from agent_comms.coordination_schema import COORDINATION_SNAPSHOT_VERSION
from agent_comms.coordination_tables.assignments import ExecutionAssignmentLink, WakeAssignment
from agent_comms.coordination_tables.attempts import AttemptRecord, ReplayAssessments, ReplayFact
from agent_comms.coordination_tables.executions import ExecutionOrigin, ExecutionRecord
from agent_comms.coordination_tables.publications import PublicationIntents, PublicationReceipt
from agent_comms.coordination_tables.recovery import ConnectivityFacet, RecoveryAudit
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.field_codec import projected


def retry_disposition_authorized(
    execution: ExecutionRecord,
    replay: ReplayAssessments | None,
    obligation: ResponseObligation | None,
) -> bool:
    """One derived terminal-failure partition; SQL view owns the same relation."""
    return (
        execution.lifecycle.current_attempt_ordinal is not None
        and execution.lifecycle.current_attempt_ordinal < execution.max_attempts
        and replay is not None
        and replay.replay_safe
        and replay.facts == ReplayFact.NONE
        and not replay.side_effects_possible
        and (
            execution.origin is not ExecutionOrigin.WIRE
            or (obligation is not None and obligation.lifecycle.retryable)
        )
    )


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """Deterministic, token-free primitive projection of one execution."""

    execution: ExecutionRecord
    attempt: AttemptRecord | None
    assignments: tuple[WakeAssignment, ...] = dataclass_field(metadata={"wire_name": "claims"})
    links: tuple[ExecutionAssignmentLink, ...] = dataclass_field(
        metadata={"snapshot_name": "execution_claims"}
    )
    replay: ReplayAssessments | None
    obligation: ResponseObligation | None
    publication_intent: PublicationIntents | None
    publication_receipt: PublicationReceipt | None
    connectivity: ConnectivityFacet | None
    last_recovery: RecoveryAudit | None
    current_execution_id: str | None
    current_attempt_ordinal: int | None
    pointer_revision: int
    is_current: bool
    snapshot_version: int = COORDINATION_SNAPSHOT_VERSION

    def require_attempt(self) -> AttemptRecord:
        if self.attempt is None:
            raise StaleFence("execution has no admitted attempt")
        return self.attempt

    def require_current_attempt(self) -> AttemptRecord:
        if not self.is_current:
            raise StaleFence("attempt fence is not current")
        return self.require_attempt()

    def __post_init__(self) -> None:
        if self.pointer_revision < 0:
            raise ValueError("pointer_revision cannot be negative")
        if self.snapshot_version != COORDINATION_SNAPSHOT_VERSION:
            raise ValueError("unsupported snapshot version")
        self.validate_membership()
        self.validate_attempt_identity()
        self.validate_current_pointer()
        self.validate_response_route()
        self.validate_publication_receipt()
        self.execution.lifecycle.validate_snapshot(
            self, retry_disposition_authorized(self.execution, self.replay, self.obligation)
        )

    def validate_membership(self) -> None:
        execution = self.execution
        execution_id = execution.execution_id
        related = (
            self.replay,
            self.obligation,
            self.publication_intent,
            self.publication_receipt,
            self.connectivity,
            self.last_recovery,
        )
        if any(
            record is not None and record.execution_id != execution_id for record in related
        ) or any(
            assignment.lifecycle.execution_id != execution_id for assignment in self.assignments
        ):
            raise IntegrityViolationError("snapshot records belong to another execution")
        assignment_ids = tuple(assignment.assignment_id for assignment in self.assignments)
        if (
            tuple(link.assignment_id for link in self.links) != assignment_ids
            or tuple(link.ordinal for link in self.links) != tuple(range(len(self.assignments)))
            or any(link.execution_id != execution_id for link in self.links)
        ):
            raise IntegrityViolationError(
                "snapshot execution-claim links disagree with ordered claims"
            )
        if len(assignment_ids) != len(set(assignment_ids)) or any(
            assignment.recipient_lookup != execution.owner_lookup for assignment in self.assignments
        ):
            raise IntegrityViolationError("snapshot claims have duplicate IDs or wrong owner")
        assignment_kind = execution.lifecycle.assignment_state()
        if any(
            type(assignment.lifecycle) is not assignment_kind for assignment in self.assignments
        ):
            raise IntegrityViolationError("snapshot claims disagree with execution disposition")

    def validate_attempt_identity(self) -> None:
        execution = self.execution
        execution_id = execution.execution_id
        attempt = self.attempt
        if (execution.lifecycle.current_attempt_ordinal is None) != (attempt is None):
            raise IntegrityViolationError("snapshot attempt does not match execution reference")
        if attempt is not None and (
            (attempt.execution_id, attempt.attempt_ordinal, attempt.owner_lookup)
            != (execution_id, execution.lifecycle.current_attempt_ordinal, execution.owner_lookup)
        ):
            raise IntegrityViolationError("snapshot attempt identity/owner mismatch")
        if attempt is not None and not execution.lifecycle.accepts_attempt(attempt.lifecycle):
            raise IntegrityViolationError("snapshot status/attempt phase mismatch")

    def validate_current_pointer(self) -> None:
        execution = self.execution
        execution_id = execution.execution_id
        attempt = self.attempt
        if (self.current_execution_id is None) != (self.current_attempt_ordinal is None):
            raise IntegrityViolationError("snapshot pointer tuple is incomplete")
        if self.is_current != (
            self.current_execution_id == execution_id
            and self.current_attempt_ordinal == execution.lifecycle.current_attempt_ordinal
            and execution.lifecycle.active
            and attempt is not None
            and not attempt.lifecycle.terminal
        ):
            raise IntegrityViolationError("snapshot current pointer is inconsistent")

    def validate_response_route(self) -> None:
        execution = self.execution
        if execution.origin is ExecutionOrigin.WIRE:
            if not self.assignments or self.obligation is None:
                raise IntegrityViolationError("wire snapshots require an obligation")
        elif self.obligation is not None or self.assignments:
            raise IntegrityViolationError("claimless snapshots cannot have claims or obligation")
        expected_target = execution.exact_target
        target_records = (
            *(assignment.lifecycle for assignment in self.assignments),
            self.obligation,
            self.publication_intent,
            self.publication_receipt,
        )
        if any(
            record is not None and record.exact_target != expected_target
            for record in target_records
        ):
            raise IntegrityViolationError("snapshot exact targets disagree")

    def validate_publication_receipt(self) -> None:
        obligation = self.obligation
        intent = self.publication_intent
        receipt = self.publication_receipt
        if obligation is not None:
            obligation.lifecycle.validate_publication(intent, receipt)
        elif intent is not None or receipt is not None:
            raise IntegrityViolationError("publication requires an obligation")
        if receipt is not None:
            if intent is None or obligation is None:
                raise IntegrityViolationError("published receipt requires intent and obligation")
            if (obligation.lifecycle.receipt_message_id, obligation.lifecycle.receipt_seq) != (
                receipt.message_id,
                receipt.seq,
            ):
                raise IntegrityViolationError("obligation receipt does not match publication")
            expected_envelope = (
                intent.publication_key,
                intent.expected_message_id,
                intent.sender,
                intent.exact_target,
                intent.message_type,
                intent.notice,
                intent.timestamp,
                intent.payload_digest,
            )
            actual_envelope = (
                receipt.publication_key,
                receipt.message_id,
                receipt.sender,
                receipt.exact_target,
                receipt.message_type,
                receipt.notice,
                receipt.timestamp,
                receipt.payload_digest,
            )
            if actual_envelope != expected_envelope:
                raise IntegrityViolationError("receipt envelope does not match frozen intent")

    @projected(view="snapshot")
    def can_retry(self) -> bool:
        execution = self.execution
        attempt = self.attempt
        return (
            retry_disposition_authorized(execution, self.replay, self.obligation)
            and execution.lifecycle.retry
            and attempt is not None
            and attempt.lifecycle.failed
            and attempt.lifecycle.backend_done
            and attempt.lifecycle.process_dead
            and attempt.lifecycle.lease_expires_at_ms is None
            and not self.is_current
        )

    def settle(self, session, outcome, reason_code: str | None) -> None:
        from .coordination_tables.attempts import AttemptRecord
        from .coordination_tables.assignments import WakeAssignment
        from .coordination_tables.executions import ExecutionRecord, CurrentExecutions
        from .coordination_tables.responses import ResponseObligation

        db = session._connection
        execution, attempt = self.execution, self.attempt
        if self.publication_intent is not None:
            raise PublicationUncertain("publication requires bus-keyed receipt resolution")
        if (
            attempt is None
            or not self.is_current
            or not (attempt.lifecycle.backend_done and attempt.lifecycle.process_dead)
        ):
            raise RecoveryBlocked("settlement requires exact final done/death evidence")
        execution_state, response = outcome.disposition(self, attempt)
        now = session.now(max(execution.updated_at_ms, attempt.updated_at_ms))
        AttemptRecord.update(
            db,
            where="execution_id=? AND attempt_ordinal=?",
            parameters=(execution.execution_id, attempt.attempt_ordinal),
            lifecycle=outcome,
            revision=attempt.revision + 1,
            updated_at_ms=now,
            reason_code=reason_code,
        )
        ExecutionRecord.update(
            db,
            where="execution_id=?",
            parameters=(execution.execution_id,),
            lifecycle=execution_state,
            revision=execution.revision + 1,
            updated_at_ms=now,
            reason_code=reason_code,
        )
        if self.obligation is not None:
            obligation = self.obligation
            ResponseObligation.update(
                db,
                where="execution_id=?",
                parameters=(execution.execution_id,),
                lifecycle=response,
                revision=obligation.revision + 1,
                updated_at_ms=session.now(obligation.updated_at_ms),
                reason_code=reason_code,
            )
        for assignment in self.assignments:
            WakeAssignment.update(
                db,
                where="assignment_id=?",
                parameters=(assignment.assignment_id,),
                lifecycle=execution_state.assignment_state().build(
                    assignment.lifecycle.mode,
                    assignment.lifecycle.execution_id,
                    assignment.lifecycle.exact_target,
                ),
                revision=assignment.revision + 1,
                updated_at_ms=session.now(assignment.updated_at_ms),
            )
        CurrentExecutions.update(
            db,
            where="owner_lookup=?",
            parameters=(execution.owner_lookup,),
            execution_id=None,
            attempt_ordinal=None,
            pointer_revision=self.pointer_revision + 1,
        )

    def require_nonpublication_response(self) -> None:
        if self.obligation is not None and not self.obligation.lifecycle.retryable:
            raise IdentityConflict("wire completion requires nonpublication obligation")

    @property
    def retry_authorized(self) -> bool:
        return retry_disposition_authorized(self.execution, self.replay, self.obligation)
