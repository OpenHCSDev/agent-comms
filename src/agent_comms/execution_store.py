"""Execution store: owned coordinator state and transitions."""

from __future__ import annotations

from agent_comms.assignment_states import EngagedAssignment
from agent_comms.assignment_store import AssignmentStore
from agent_comms.coordination_contracts import (
    _bounded_reason,
)
from agent_comms.coordination_errors import (
    IdentityConflict,
    StaleRevision,
)
from agent_comms.coordination_results import AlreadyApplied, Applied
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordination_snapshot import RecoverySnapshot
from agent_comms.coordination_tables.assignments import ExecutionAssignmentLink, WakeAssignment
from agent_comms.coordination_tables.executions import (
    ExecutionOrigin,
    ExecutionRecord,
)
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.execution_states import (
    FailedExecution,
    PendingExecution,
    QueuedExecution,
)
from agent_comms.obligation_states import (
    FailedResponse,
    PendingResponse,
)
from agent_comms.participant_store import ParticipantStore
from agent_comms.recovery_reader import RecoveryReader


class ExecutionStore:
    def __init__(
        self,
        session: CoordinationSession,
        participants: ParticipantStore,
        assignments: AssignmentStore,
        snapshots: RecoveryReader,
    ) -> None:
        self.session = session
        self.participants = participants
        self.assignments = assignments
        self.snapshots = snapshots

    def create(
        self,
        execution_id: str,
        origin: ExecutionOrigin,
        owner_lookup: str,
        owner_thread: str,
        max_attempts: int,
        *,
        assignment_ids: tuple[str, ...] = (),
        exact_target: str | None = None,
    ) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
        origin = ExecutionOrigin(origin)
        if origin is ExecutionOrigin.WIRE and (not assignment_ids or not exact_target):
            raise IdentityConflict("wire execution requires ordered claims and target")
        if origin is not ExecutionOrigin.WIRE and (assignment_ids or exact_target is not None):
            raise IdentityConflict("claimless execution cannot bind claims")
        with self.session.transaction() as db:
            row = ExecutionRecord.one(self.session._connection, execution_id=execution_id)
            if row is not None:
                snapshot = self.snapshots.get(execution_id)
                e = snapshot.execution
                if (
                    e.origin,
                    e.owner_lookup,
                    e.owner_thread,
                    e.max_attempts,
                    e.exact_target,
                    tuple(link.assignment_id for link in snapshot.links),
                ) != (
                    origin,
                    owner_lookup,
                    owner_thread,
                    max_attempts,
                    exact_target,
                    assignment_ids,
                ):
                    raise IdentityConflict("execution identity conflicts")
                return AlreadyApplied(snapshot)
            participant = self.participants.get(owner_lookup)
            if not participant.committed or participant.owner_thread != owner_thread:
                raise IdentityConflict("execution requires committed current owner")
            if not isinstance(max_attempts, int) or max_attempts <= 0:
                raise ValueError("max_attempts must be positive")
            if len(set(assignment_ids)) != len(assignment_ids):
                raise IdentityConflict("duplicate claim membership")
            now = self.session.now()
            ExecutionRecord(
                execution_id=execution_id,
                origin=origin,
                lifecycle=QueuedExecution(),
                exact_target=exact_target,
                owner_thread=owner_thread,
                owner_lookup=owner_lookup,
                revision=1,
                max_attempts=max_attempts,
                reason_code=None,
                created_at_ms=now,
                updated_at_ms=now,
            ).insert(db)
            for ordinal, assignment_id in enumerate(assignment_ids):
                assignment = self.assignments.get(assignment_id)
                if (
                    assignment.recipient_lookup != owner_lookup
                    or assignment.lifecycle.execution_id is not None
                    or not assignment.lifecycle.engageable
                ):
                    raise IdentityConflict("claim cannot engage this execution")
                decision = EngagedAssignment.build(
                    assignment.lifecycle.mode, execution_id, exact_target
                )
                WakeAssignment.update(
                    db,
                    where="assignment_id=?",
                    parameters=(assignment_id,),
                    lifecycle=decision,
                    revision=assignment.revision + 1,
                    updated_at_ms=self.session.now(assignment.updated_at_ms),
                )
                ExecutionAssignmentLink(execution_id, assignment_id, ordinal).insert(db)
            if origin is ExecutionOrigin.WIRE:
                ResponseObligation(
                    execution_id,
                    exact_target,
                    PendingResponse(),
                    None,
                    now,
                    now,
                    1,
                ).insert(db)
            return Applied(self.snapshots.get(execution_id))

    def mark_pending(
        self, execution_id: str, *, expected_revision: int
    ) -> Applied[RecoverySnapshot]:
        with self.session.transaction() as db:
            snapshot = self.snapshots.get(execution_id)
            if snapshot.execution.revision != expected_revision:
                raise StaleRevision("execution revision changed")
            if not snapshot.execution.lifecycle.queued:
                raise IdentityConflict("only queued executions can become pending")
            ExecutionRecord.update(
                db,
                where="execution_id=? AND revision=?",
                parameters=(execution_id, expected_revision),
                lifecycle=PendingExecution(),
                revision=expected_revision + 1,
                updated_at_ms=self.session.now(snapshot.execution.updated_at_ms),
            )
            return Applied(self.snapshots.get(execution_id))

    def fail_unstarted(
        self,
        execution_id: str,
        *,
        expected_revision: int,
        reason_code: str,
    ) -> Applied[RecoverySnapshot]:
        """Fail queued/pending work; v2 cannot resume a pre-attempt deferral."""
        _bounded_reason(reason_code)
        if not reason_code:
            raise ValueError("unstarted settlement requires a reason")
        with self.session.transaction() as db:
            before = self.snapshots.get(execution_id)
            execution = before.execution
            if execution.revision != expected_revision:
                raise StaleRevision("execution revision changed")
            if not execution.lifecycle.unstarted:
                raise IdentityConflict("unstarted failure requires queued/pending work")
            now = self.session.now(execution.updated_at_ms)
            ExecutionRecord.update(
                db,
                where="execution_id=?",
                parameters=(execution_id,),
                lifecycle=FailedExecution(),
                revision=execution.revision + 1,
                reason_code=reason_code,
                updated_at_ms=now,
            )
            if before.obligation is not None:
                ResponseObligation.update(
                    db,
                    where="execution_id=?",
                    parameters=(execution_id,),
                    lifecycle=FailedResponse(),
                    revision=before.obligation.revision + 1,
                    reason_code=reason_code,
                    updated_at_ms=self.session.now(before.obligation.updated_at_ms),
                )
            for assignment in before.assignments:
                WakeAssignment.update(
                    db,
                    where="assignment_id=?",
                    parameters=(assignment.assignment_id,),
                    lifecycle=FailedExecution.assignment_state().build(
                        assignment.lifecycle.mode,
                        assignment.lifecycle.execution_id,
                        assignment.lifecycle.exact_target,
                    ),
                    revision=assignment.revision + 1,
                    updated_at_ms=self.session.now(assignment.updated_at_ms),
                )
            return Applied(self.snapshots.get(execution_id))
