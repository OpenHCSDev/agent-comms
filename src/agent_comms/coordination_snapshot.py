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
from agent_comms.coordination_tables.attempts import AttemptRecord, ReplayAssessments
from agent_comms.coordination_tables.executions import ExecutionOrigin, ExecutionRecord
from agent_comms.coordination_tables.publications import PublicationIntents, PublicationReceipt
from agent_comms.coordination_tables.recovery import ConnectivityFacet, RecoveryAudit
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.field_codec import projected


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
    obligations: tuple[ResponseObligation, ...]
    publication_intents: tuple[PublicationIntents, ...]
    publication_receipts: tuple[PublicationReceipt, ...]
    connectivity: ConnectivityFacet | None
    last_recovery: RecoveryAudit | None
    current_execution_id: str | None
    current_attempt_ordinal: int | None
    pointer_revision: int
    is_current: bool
    snapshot_version: int = COORDINATION_SNAPSHOT_VERSION

    def require_wire_responses(self) -> tuple[ResponseObligation, ...]:
        if not self.assignments or not self.obligations:
            raise IdentityConflict("wire response requires original claims and obligations")
        return self.obligations

    def require_wire_response(self, exact_target: str) -> ResponseObligation:
        obligation = ResponseObligation.response_record(self.require_wire_responses(), exact_target)
        if obligation is None:
            raise IdentityConflict("response route is absent from original obligations")
        return obligation

    def require_final_response(self, recipient_lookup: str, exact_target: str) -> ResponseObligation:
        if self.execution.owner_lookup != recipient_lookup:
            raise StaleFence("response turn belongs to a different SQL recipient")
        self.execution.lifecycle.require_final_response()
        self.require_attempt().lifecycle.require_final_response()
        return self.require_wire_response(exact_target)

    def require_preparation(self, exact_target: str) -> ResponseObligation:
        obligation = self.require_wire_response(exact_target)
        obligation.lifecycle.require_preparation()
        return obligation

    def require_existing_preparation(self, candidate) -> None:
        obligation = self.require_wire_response(candidate.target)
        obligation.lifecycle.require_existing_preparation()
        intent = ResponseObligation.response_record(self.publication_intents, candidate.target)
        if intent is None or not intent.matches_request(self.execution.execution_id, candidate):
            raise IdentityConflict("prepared response envelope conflicts")

    def require_publishing_intent(self, exact_target: str) -> tuple[PublicationIntents, ResponseObligation]:
        if len(self.publication_intents) != len(self.require_wire_responses()):
            raise IdentityConflict("all original response bodies must be frozen before first append")
        obligation = self.require_wire_response(exact_target)
        obligation.lifecycle.require_publishing()
        intent = ResponseObligation.response_record(self.publication_intents, exact_target)
        if intent is None or intent.sender != self.execution.owner_thread:
            raise IdentityConflict("no frozen publishing intent for current owner")
        return intent, obligation

    def require_published_evidence(self, exact_target: str):
        intent = ResponseObligation.response_record(self.publication_intents, exact_target)
        receipt = ResponseObligation.response_record(self.publication_receipts, exact_target)
        if intent is None or receipt is None:
            raise StaleFence("response route has no frozen publication evidence")
        return intent, receipt

    def completed_response(self) -> bool:
        return self.execution.lifecycle.completed

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
        self.execution.lifecycle.validate_snapshot(self, self.retry_authorized)

    def validate_membership(self) -> None:
        execution = self.execution
        execution_id = execution.execution_id
        related = (
            self.replay,
            *self.obligations,
            *self.publication_intents,
            *self.publication_receipts,
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
        if self.execution.origin is ExecutionOrigin.WIRE:
            if not self.assignments or not self.obligations:
                raise IntegrityViolationError("wire snapshots require original obligations")
        elif self.obligations or self.assignments:
            raise IntegrityViolationError("claimless snapshots cannot have claims or obligations")
        expected = {row.lifecycle.exact_target for row in self.assignments}
        targets = tuple(row.exact_target for row in self.obligations)
        if len(set(targets)) != len(targets) or set(targets) != expected:
            raise IntegrityViolationError("response obligations differ from original claim routes")
        for records in (self.publication_intents, self.publication_receipts):
            routes = tuple(row.exact_target for row in records)
            if len(set(routes)) != len(routes) or not set(routes) <= expected:
                raise IntegrityViolationError("publication routes differ from original obligations")

    def validate_publication_receipt(self) -> None:
        for obligation in self.obligations:
            target = obligation.exact_target
            intent = ResponseObligation.response_record(self.publication_intents, target)
            receipt = ResponseObligation.response_record(self.publication_receipts, target)
            obligation.lifecycle.validate_publication(intent, receipt)

    @projected(view="snapshot")
    def can_retry(self) -> bool:
        return self.execution.lifecycle.can_retry(
            authorized=self.retry_authorized,
            attempt=self.attempt.lifecycle if self.attempt is not None else None,
            is_current=self.is_current,
        )

    def settle(self, session, outcome, reason_code: str | None) -> None:
        from .coordination_tables.attempts import AttemptRecord
        from .coordination_tables.assignments import WakeAssignment
        from .coordination_tables.executions import ExecutionRecord, CurrentExecutions
        from .coordination_tables.responses import ResponseObligation

        db = session._connection
        execution, attempt = self.execution, self.attempt
        if self.publication_intents:
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
        for obligation in self.obligations:
            ResponseObligation.update(
                db,
                where="execution_id=? AND exact_target=?",
                parameters=(execution.execution_id, obligation.exact_target),
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

    def finish_publications(self, session) -> None:
        """Release this one native attempt only when every original route settled."""
        from .attempt_states import SucceededAttempt
        from .execution_states import CompletedExecution
        from .coordination_tables.executions import CurrentExecutions

        if not all(row.lifecycle.published for row in self.require_wire_responses()):
            return
        attempt = self.require_current_attempt()
        db, execution = session._connection, self.execution
        AttemptRecord.update(
            db, where="execution_id=? AND attempt_ordinal=? AND revision=?",
            parameters=(execution.execution_id, attempt.attempt_ordinal, attempt.revision),
            lifecycle=SucceededAttempt(), revision=attempt.revision + 1,
            updated_at_ms=session.now(attempt.updated_at_ms),
        )
        ExecutionRecord.update(
            db, where="execution_id=? AND revision=?",
            parameters=(execution.execution_id, execution.revision),
            lifecycle=CompletedExecution(attempt.attempt_ordinal),
            revision=execution.revision + 1, updated_at_ms=session.now(execution.updated_at_ms),
        )
        for assignment in self.assignments:
            WakeAssignment.update(
                db, where="assignment_id=? AND revision=?",
                parameters=(assignment.assignment_id, assignment.revision),
                lifecycle=CompletedExecution.assignment_state().build(
                    assignment.lifecycle.mode, assignment.lifecycle.execution_id,
                    assignment.lifecycle.exact_target,
                ), revision=assignment.revision + 1,
                updated_at_ms=session.now(assignment.updated_at_ms),
            )
        CurrentExecutions.update(
            db, where="owner_lookup=? AND pointer_revision=? AND execution_id=?",
            parameters=(execution.owner_lookup, self.pointer_revision, execution.execution_id),
            execution_id=None, attempt_ordinal=None, pointer_revision=self.pointer_revision + 1,
        )

    def require_nonpublication_response(self) -> None:
        for obligation in self.obligations:
            obligation.lifecycle.require_nonpublication()

    @property
    def retry_authorized(self) -> bool:
        return self.execution.retry_authorized(self.replay, self.obligations)
