"""Execution store: owned coordinator state and transitions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_comms.reason_codes import GivenReasonCode, NoReasonCode
from agent_comms.assignment_states import EngagedAssignment
from agent_comms.coordination_errors import (
    IdentityConflict,
    StaleRevision,
)
from agent_comms.coordination_results import AlreadyApplied, Applied
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
from agent_comms.recovery_reader import RecoveryReader

if TYPE_CHECKING:
    from agent_comms.selected_source_batch import SelectedSource


class ExecutionStore(RecoveryReader):
    def create(
        self,
        execution_id: str,
        origin: ExecutionOrigin,
        owner_lookup: str,
        max_attempts: int,
        *,
        sources: tuple[SelectedSource, ...] = (),
    ) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
        with self.session.writing() as db:
            assignments = tuple(source.assignment for source in sources)
            return self._create(db, execution_id, origin, owner_lookup, max_attempts, sources, assignments)

    def create_after_triage(
        self,
        execution_id: str,
        origin: ExecutionOrigin,
        owner_lookup: str,
        max_attempts: int,
        *,
        sources: tuple[SelectedSource, ...],
        settled: tuple[WakeAssignment, ...],
    ) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
        """Engage exactly the rows witnessed by the original triage proof commit."""
        with self.session.writing() as db:
            assignments = tuple(source.assignment for source in sources)
            if assignments != settled:
                raise StaleRevision("original claims changed after proved native settlement")
            return self._create(db, execution_id, origin, owner_lookup, max_attempts, sources, assignments)

    def _create(self, db, execution_id, origin, owner_lookup,
                max_attempts, sources, assignments):
        """The single creation algorithm, under its caller's owning transaction."""
        origin = ExecutionOrigin(origin)
        for source in sources:
            if source.store is not self.assignments:
                raise IdentityConflict("execution source belongs to another assignment store")
        assignment_ids = tuple(source.assignment_id for source in sources)
        if origin is ExecutionOrigin.WIRE and not sources:
            raise IdentityConflict("wire execution requires ordered original sources")
        if origin is not ExecutionOrigin.WIRE and sources:
            raise IdentityConflict("claimless execution cannot bind claims")
        row = ExecutionRecord.one(self.session._connection, execution_id=execution_id)
        if row is not None:
            snapshot = self.get(execution_id)
            e = snapshot.execution
            if (
                e.origin,
                e.owner_lookup,
                e.max_attempts,
                tuple(sorted(row.exact_target for row in snapshot.obligations)),
                tuple(link.assignment_id for link in snapshot.links),
            ) != (
                origin,
                owner_lookup,
                max_attempts,
                tuple(sorted({source.delivery.message.exact_reply_target for source in sources})),
                assignment_ids,
            ):
                raise IdentityConflict("execution identity conflicts")
            return AlreadyApplied(snapshot)
        participant = self.participants.get(owner_lookup)
        if not participant.committed:
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
            owner_lookup=owner_lookup,
            revision=1,
            max_attempts=max_attempts,
            reason_code=NoReasonCode(),
            created_at_ms=now,
            updated_at_ms=now,
        ).insert(db)
        routes = {}
        for ordinal, (source, assignment) in enumerate(zip(sources, assignments, strict=True)):
            assignment_id = source.assignment_id
            exact_target = source.delivery.message.exact_reply_target
            routes.setdefault(exact_target, None)
            assignment.lifecycle.require_engageable()
            if (
                assignment.recipient_lookup != owner_lookup
                or assignment.lifecycle.execution_id is not None
            ):
                raise IdentityConflict("claim cannot engage this execution")
            decision = EngagedAssignment.build(
                assignment.lifecycle.mode, execution_id, exact_target
            )
            updated = WakeAssignment.update(
                db,
                where="assignment_id=? AND revision=?",
                parameters=(assignment_id, assignment.revision),
                lifecycle=decision,
                revision=assignment.revision + 1,
                updated_at_ms=self.session.now(assignment.updated_at_ms),
            )
            if updated.rowcount != 1:
                raise StaleRevision("original claim changed during engagement")
            ExecutionAssignmentLink(execution_id, assignment_id, ordinal).insert(db)
        for exact_target in routes:
            ResponseObligation(
                execution_id,
                exact_target,
                PendingResponse(),
                None,
                now,
                now,
                1,
            ).insert(db)
        return Applied(self.get(execution_id))

    def mark_pending(
        self, execution_id: str, *, expected_revision: int
    ) -> Applied[RecoverySnapshot]:
        with self.session.writing() as db:
            snapshot = self.get(execution_id)
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
            return Applied(self.get(execution_id))

    def fail_unstarted(
        self,
        execution_id: str,
        *,
        expected_revision: int,
        reason_code: GivenReasonCode,
    ) -> Applied[RecoverySnapshot]:
        """Fail queued/pending work; v2 cannot resume a pre-attempt deferral."""
        with self.session.writing() as db:
            before = self.get(execution_id)
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
            for obligation in before.obligations:
                ResponseObligation.update(
                    db,
                    where="execution_id=? AND exact_target=?",
                    parameters=(execution_id, obligation.exact_target),
                    lifecycle=FailedResponse(),
                    revision=obligation.revision + 1,
                    reason_code=reason_code,
                    updated_at_ms=self.session.now(obligation.updated_at_ms),
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
            return Applied(self.get(execution_id))
