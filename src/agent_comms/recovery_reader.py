"""Recovery reader: owned coordinator state and transitions."""

from __future__ import annotations

from agent_comms.assignment_store import AssignmentStore
from agent_comms.coordination_errors import (
    IdentityConflict,
    IntegrityViolationError,
)
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordination_snapshot import RecoverySnapshot
from agent_comms.coordination_tables.assignments import ExecutionAssignmentLink
from agent_comms.coordination_tables.attempts import AttemptRecord, ReplayAssessments
from agent_comms.coordination_tables.executions import (
    ExecutionRecord,
)
from agent_comms.coordination_tables.publications import (
    PublicationIntents,
    PublicationReceipt,
    PublicationReceipts,
)
from agent_comms.coordination_tables.recovery import (
    ConnectivityFacet,
    RecoveryAudit,
)
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.participant_store import ParticipantStore


class RecoveryReader:
    def __init__(
        self,
        session: CoordinationSession,
        participants: ParticipantStore,
        assignments: AssignmentStore,
    ) -> None:
        self.session = session
        self.participants = participants
        self.assignments = assignments

    def get(self, execution_id: str) -> RecoverySnapshot:
        with self.session.read():
            db = self.session._connection
            row = ExecutionRecord.one(self.session._connection, execution_id=execution_id)
            if row is None:
                raise IdentityConflict("unknown execution")
            execution = row
            ordinal = execution.lifecycle.current_attempt_ordinal
            attempt = (
                AttemptRecord.one(db, execution_id=execution_id, attempt_ordinal=ordinal)
                if ordinal is not None
                else None
            )
            links = tuple(
                ExecutionAssignmentLink.select(
                    db, where="execution_id=?", parameters=(execution_id,), order_by=("ordinal",)
                )
            )
            assignments = tuple(self.assignments.get(link.assignment_id) for link in links)
            replay = ReplayAssessments.one(db, execution_id=execution_id)
            obligation = ResponseObligation.one(db, execution_id=execution_id)
            # Read-only projections do not grant Tx1, append, Tx2 or resolution.
            intent = PublicationIntents.one(db, execution_id=execution_id)
            receipt_row = PublicationReceipts.one(db, execution_id=execution_id)
            if receipt_row is not None and intent is None:
                raise IntegrityViolationError("publication receipt has no frozen intent")
            receipt = None
            if receipt_row is not None:
                projection = ", ".join(
                    f"{'r' if name in PublicationReceipts.columns() else 'i'}.{name}"
                    for name in PublicationReceipt.columns()
                )
                (receipt,) = PublicationReceipt.read(
                    db.execute(
                        f"SELECT {projection} FROM {PublicationReceipts.declared_name} r "
                        f"JOIN {PublicationIntents.declared_name} i ON r.execution_id=i.execution_id "
                        "WHERE r.execution_id=?",
                        (execution_id,),
                    )
                )
            connectivity = ConnectivityFacet.one(db, execution_id=execution_id)
            audit = next(
                iter(
                    RecoveryAudit.read(
                        db.execute(
                            f"SELECT * FROM {RecoveryAudit.declared_name} WHERE execution_id=? "
                            "ORDER BY audit_id DESC LIMIT 1",
                            (execution_id,),
                        )
                    )
                ),
                None,
            )
            pointer = self.participants.get(execution.owner_lookup).pointer
            return RecoverySnapshot(
                execution,
                attempt,
                assignments,
                links,
                replay,
                obligation,
                intent,
                receipt,
                connectivity,
                audit,
                pointer.execution_id,
                pointer.attempt_ordinal,
                pointer.pointer_revision,
                pointer.execution_id == execution_id,
            )
