"""Assignment store: owned coordinator state and transitions."""

from __future__ import annotations

from dataclasses import replace

from agent_comms.assignment_states import AssignmentState
from agent_comms.coordination_contracts import (
    POLICY_VERSION,
    RESOLVER_VERSION,
)
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
        if (
            assignment.revision != 1
            or assignment.lifecycle.execution_id is not None
            or assignment.lifecycle.exact_target is not None
            or assignment.updated_at_ms != assignment.accepted_at_ms
            or assignment.lifecycle.verdict is not None
            or type(assignment.lifecycle) is not assignment.lifecycle.mode.initial_state()
            or assignment.resolver_version != RESOLVER_VERSION
            or assignment.policy_version != POLICY_VERSION
        ):
            raise IdentityConflict("claim acceptance requires initial frozen decision")
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
                immutable = (
                    "assignment_id",
                    "recipient",
                    "recipient_lookup",
                    "wire_seq",
                    "message_id",
                    "audience",
                    "resolver_version",
                    "policy_version",
                    "accepted_at_ms",
                )
                if current.lifecycle.mode != assignment.lifecycle.mode or any(
                    getattr(current, name) != getattr(assignment, name) for name in immutable
                ):
                    raise IdentityConflict("accepted claim identity conflicts")
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
            if (
                current.lifecycle.execution_id is not None
                or not disposition.preengagement_target
                or disposition not in current.lifecycle.successors()
            ):
                raise IdentityConflict("preengagement transition is not declared")
            after = replace(
                current,
                updated_at_ms=self.session.now(current.updated_at_ms),
                revision=current.revision + 1,
                lifecycle=disposition.build(
                    current.lifecycle.mode,
                    current.lifecycle.execution_id,
                    current.lifecycle.exact_target,
                ),
            )
            if after.lifecycle.mode != current.lifecycle.mode:
                raise IdentityConflict("preengagement cannot change frozen wake policy")
            WakeAssignment.update(
                db,
                where="assignment_id=? AND revision=?",
                parameters=(assignment_id, expected_revision),
                lifecycle=after.lifecycle,
                updated_at_ms=after.updated_at_ms,
                revision=after.revision,
            )
            return Applied(after)
