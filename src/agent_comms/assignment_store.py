"""Assignment store: owned coordinator state and transitions."""

from __future__ import annotations

from dataclasses import replace

from agent_comms.assignment_states import AssignmentState
from agent_comms.coordination_errors import (
    IdentityConflict,
    StaleRevision,
)
from agent_comms.coordination_results import AlreadyApplied, Applied
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordination_tables.assignments import WakeAssignment


class AssignmentStore:
    def __init__(self, session: CoordinationSession) -> None:
        self.session = session

    def get(self, assignment_id: str) -> WakeAssignment:
        row = WakeAssignment.one(self.session._connection, assignment_id=assignment_id)
        if row is None:
            raise IdentityConflict("unknown claim")
        return row

    def accept(
        self, assignment: WakeAssignment
    ) -> Applied[WakeAssignment] | AlreadyApplied[WakeAssignment]:
        assignment.require_initial_decision()
        with self.session.transaction() as db:
            rows = WakeAssignment.read(
                db.execute(
                    (
                        "SELECT * FROM wake_claims WHERE assignment_id=? OR (recipient_lookup="
                        "? AND wire_seq=?) LIMIT 1"
                    ),
                    (assignment.assignment_id, assignment.recipient_lookup, assignment.wire_seq),
                )
            )
            row = next(iter(rows), None)
            if row is not None:
                current = row
                current.require_same_acceptance(assignment)
                return AlreadyApplied(current)
            assignment.insert(db)
            return Applied(assignment)

    def transition_preengagement(
        self,
        assignment_id: str,
        disposition: type[AssignmentState],
        *,
        expected_revision: int,
    ) -> Applied[WakeAssignment]:
        with self.session.transaction() as db:
            current = self.get(assignment_id)
            if current.revision != expected_revision:
                raise StaleRevision("claim revision changed")
            lifecycle = current.lifecycle.preengagement(disposition)
            after = replace(
                current,
                updated_at_ms=self.session.now(current.updated_at_ms),
                revision=current.revision + 1,
                lifecycle=lifecycle,
            )
            WakeAssignment.update(
                db,
                where="assignment_id=? AND revision=?",
                parameters=(assignment_id, expected_revision),
                lifecycle=after.lifecycle,
                updated_at_ms=after.updated_at_ms,
                revision=after.revision,
            )
            return Applied(after)
