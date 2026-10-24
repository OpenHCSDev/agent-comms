"""Assignment store: owned coordinator state and transitions."""

from __future__ import annotations

from agent_comms.field_codec import ThreadLookup

from dataclasses import replace

from agent_comms.assignment_states import AssignmentState, DeferredAssignment
from agent_comms.message_reference import MessageReference
from agent_comms.coordination_errors import (
    IdentityConflict,
    StaleRevision,
)
from agent_comms.coordination_results import AlreadyApplied, Applied
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.typed_table import sql_literal


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
        with self.session.writing() as db:
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

    def recoverable_unbound_deliveries(
        self, recipient_lookup: ThreadLookup, sources: frozenset[MessageReference],
    ) -> tuple[WakeAssignment, ...]:
        """Paused following deliveries with neither send binding nor native source."""
        from agent_comms.native_runtime_input import NativeRuntimeInput

        if not sources:
            return ()
        with self.session.read():
            db = self.session._connection
            deferred = WakeAssignment.read(db.execute(
                "SELECT * FROM wake_claims claims WHERE recipient_lookup=? "
                f"AND disposition={sql_literal(DeferredAssignment)} AND execution_id IS NULL "
                f"AND NOT EXISTS (SELECT 1 FROM ({NativeRuntimeInput.source_membership_sql()}) "
                "sources WHERE sources.assignment_id=claims.assignment_id)",
                (recipient_lookup,),
            ))
            return tuple(
                current for current in deferred
                if current.source in sources
                and current.lifecycle
                == DeferredAssignment.build(current.lifecycle.mode, None, None)
            )

    def restore_unbound_delivery(self, captured: WakeAssignment) -> WakeAssignment:
        """Return one proved-not-sent following delivery to its declared initial state."""
        current = self.get(captured.assignment_id)
        if current != captured:
            raise StaleRevision("unbound delivery assignment changed")
        return self.transition_preengagement(
            current.assignment_id,
            current.lifecycle.mode.initial_state(),
            expected_revision=current.revision,
        ).value

    def transition_preengagement(
        self,
        assignment_id: str,
        disposition: type[AssignmentState],
        *,
        expected_revision: int,
    ) -> Applied[WakeAssignment]:
        with self.session.writing() as db:
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
