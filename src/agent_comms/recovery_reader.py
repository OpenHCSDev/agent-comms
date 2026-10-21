"""Recovery reader: owned coordinator state and transitions."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.assignment_store import AssignmentStore
from agent_comms.coordination_errors import (
    IdentityConflict,
)
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordination_snapshot import RecoverySnapshot
from agent_comms.coordination_tables.assignments import ExecutionAssignmentLink
from agent_comms.coordination_tables.attempts import AttemptRecord
from agent_comms.coordination_tables.executions import ReplayAssessments
from agent_comms.coordination_tables.executions import (
    ExecutionRecord,
)
from agent_comms.coordination_tables.publications import (
    PublicationIntents,
    PublicationEvidence,
)
from agent_comms.coordination_tables.recovery import (
    ConnectivityFacet,
    RecoveryAudit,
)
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.participant_store import ParticipantStore


@dataclass(eq=False, repr=False)
class RecoveryReader:
    session: CoordinationSession
    participants: ParticipantStore
    assignments: AssignmentStore

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
            obligations = tuple(ResponseObligation.select(db, where="execution_id=?",
                parameters=(execution_id,), order_by=("exact_target",)))
            intents = tuple(PublicationIntents.select(db, where="execution_id=?",
                parameters=(execution_id,), order_by=("exact_target",)))
            evidence = tuple(PublicationEvidence.select(db, where="execution_id=?",
                parameters=(execution_id,), order_by=("exact_target",)))
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
                obligations,
                intents,
                evidence,
                connectivity,
                audit,
                pointer.execution_id,
                pointer.attempt_ordinal,
                pointer.pointer_revision,
                pointer.execution_id.otherwise(None) == execution_id,
            )
