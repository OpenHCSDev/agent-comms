"""Opt-in committed-bus to ONE-transaction full-N coordinator acceptance.

No live wake, context assertion, publication, retry, or cursor advancement.
Only a MessageBus-owned original raw-row read can enter this writer. A frozen
pure digest, caller-supplied DTO, or unbound singleton claim is not bus proof.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Annotated

from agent_comms.assignment_states import AssignmentState, PendingNotification
from agent_comms.bus_publication import Delivered
from agent_comms.private_registry_guard import RegistryPrivacy
from agent_comms.cohort_schema import (
    AwarenessClaimGenerations,
    ClaimBatchAcknowledgements,
    ClaimBatchMembers,
    CohortDeliveries,
    CohortTable,
    NoDeliveryClaim,
    SelectedClaim,
    assert_optional_awareness_schema,
)
from agent_comms.coordination_contracts import POLICY_VERSION, RESOLVER_VERSION
from agent_comms.coordination_errors import (
    IdentityConflict,
    IntegrityViolationError,
    SchemaVersionError,
)
from agent_comms.coordination_results import AlreadyApplied, Applied
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.participants import OwnerGenerations, Participants
from agent_comms.coordinator import Coordination

from .field_codec import AssignmentId, MalformedRecord, MessageId, WireName, RootId, ThreadLookup

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AcceptedCohort:
    wire_root_id: RootId
    wire_seq: int
    message_id: MessageId
    member_count: int
    assignment_count: Annotated[int, WireName("claim_count")]
    accepted_at_ms: int
    assignments: Annotated[tuple[WakeAssignment, ...], WireName("claims")]

    @classmethod
    def record(
        cls,
        initial: Delivered,
        privacy: RegistryPrivacy,
        store: Coordination,
        db: sqlite3.Connection,
    ) -> Applied[AcceptedCohort] | AlreadyApplied[AcceptedCohort]:
        """Record one bus original inside the caller's acceptance transaction."""
        CohortTable.require_installed(db)
        acknowledgement = ClaimBatchAcknowledgements.one(
            db, wire_root_id=privacy.namespace, wire_seq=initial.message.seq
        )
        if acknowledgement is not None:
            return AlreadyApplied(_claim_record(db, initial, privacy))
        if WakeAssignment.select(db, where="wire_seq=?", parameters=(initial.message.seq,)):
            raise IdentityConflict("singleton claim cannot be adopted by a cohort")
        if ClaimBatchAcknowledgements.select(
            db, where="wire_seq=?", parameters=(initial.message.seq,)
        ):
            raise IdentityConflict("wire sequence already belongs to another cohort root")
        for recipient in initial.recipients:
            if Participants.one(db, participant_lookup=recipient.recipient_lookup) is None:
                raise IdentityConflict("frozen recipient has no durable participant identity")
        accepted_at = store.session.now()
        expected = _expected_assignments(initial, accepted_at, privacy)
        ClaimBatchAcknowledgements.for_delivery(initial, privacy, accepted_at).insert(db)
        for ordinal, assignment in enumerate(expected):
            assignment.insert(db)
            ClaimBatchMembers(
                wire_root_id=privacy.namespace,
                wire_seq=initial.message.seq,
                ordinal=ordinal,
                claim_id=assignment.assignment_id,
                recipient_lookup=assignment.recipient_lookup,
            ).insert(db)
        selected = {
            assignment.recipient_lookup: SelectedClaim(assignment.assignment_id)
            for assignment in expected
        }
        for ordinal, recipient in enumerate(initial.recipients):
            CohortDeliveries(
                wire_root_id=privacy.namespace,
                wire_seq=initial.message.seq,
                ordinal=ordinal,
                recipient_lookup=recipient.recipient_lookup,
                canonical_thread=recipient.canonical_thread,
                claim_id=selected.get(recipient.recipient_lookup, NoDeliveryClaim()),
            ).insert(db)
        _record_optional_owner_generations(db, expected, privacy.namespace, initial.message.seq)
        ClaimBatchAcknowledgements.update(
            db,
            where="wire_root_id=? AND wire_seq=?",
            parameters=(privacy.namespace, initial.message.seq),
            sealed=True,
        )
        return Applied(_claim_record(db, initial, privacy))


def _assignment_id(root: str, seq: int, lookup: ThreadLookup) -> AssignmentId:
    identity = f"agent-comms:cohort-claim:v1\0{root}:{seq}:{lookup}".encode()
    return AssignmentId.cohort(identity)


def _expected_assignments(
    initial: Delivered, accepted_at_ms: int, privacy: RegistryPrivacy
) -> tuple[WakeAssignment, ...]:
    result: list[WakeAssignment] = []
    for recipient, member in zip(initial.recipients, initial.decisions, strict=True):
        if recipient.recipient_lookup != member.recipient:
            raise IntegrityViolationError("bus decision does not match frozen N")
        for decision in member.claim_decisions():
            result.append(
                WakeAssignment(
                    assignment_id=_assignment_id(
                        privacy.namespace, initial.message.seq, decision.recipient
                    ),
                    recipient=recipient.canonical_thread,
                    recipient_lookup=decision.recipient,
                    wire_seq=initial.message.seq,
                    message_id=initial.message.message_id,
                    audience=decision.audience,
                    accepted_at_ms=accepted_at_ms,
                    updated_at_ms=accepted_at_ms,
                    revision=1,
                    resolver_version=RESOLVER_VERSION,
                    policy_version=POLICY_VERSION,
                    lifecycle=decision.wake_policy().initial_state()(),
                )
            )
    return tuple(result)


def _claim_record(
    db: sqlite3.Connection, initial: Delivered, privacy: RegistryPrivacy,
) -> AcceptedCohort:
    message = initial.message
    namespace = privacy.namespace
    row = ClaimBatchAcknowledgements.one(db, wire_root_id=namespace, wire_seq=message.seq)
    if row is None or not row.sealed:
        raise IdentityConflict("cohort acknowledgement is absent or unsealed")
    row.require_delivery(initial, privacy)
    expected = _expected_assignments(initial, row.accepted_at_ms, privacy)
    members = ClaimBatchMembers.select(
        db, where="wire_root_id=? AND wire_seq=?",
        parameters=(namespace, message.seq), order_by=("ordinal",),
    )
    if members != [
        ClaimBatchMembers(
            wire_root_id=namespace, wire_seq=message.seq, ordinal=ordinal,
            claim_id=assignment.assignment_id, recipient_lookup=assignment.recipient_lookup,
        )
        for ordinal, assignment in enumerate(expected)
    ]:
        raise IdentityConflict("sealed K claim members conflict with committed bus")
    deliveries = CohortDeliveries.select(
        db,
        where="wire_root_id=? AND wire_seq=?",
        parameters=(namespace, message.seq),
        order_by=("ordinal",),
    )
    selected = {
        assignment.recipient_lookup: SelectedClaim(assignment.assignment_id)
        for assignment in expected
    }
    if deliveries != [
        CohortDeliveries(
            wire_root_id=namespace,
            wire_seq=message.seq,
            ordinal=ordinal,
            recipient_lookup=recipient.recipient_lookup,
            canonical_thread=recipient.canonical_thread,
            claim_id=selected.get(recipient.recipient_lookup, NoDeliveryClaim()),
        )
        for ordinal, recipient in enumerate(initial.recipients)
    ]:
        raise IdentityConflict("sealed N delivery rows conflict with committed bus")
    current: list[WakeAssignment] = []
    for accepted in expected:
        actual = WakeAssignment.one(db, assignment_id=accepted.assignment_id)
        if actual is None:
            raise IdentityConflict("sealed cohort claim is missing")
        accepted.require_same_acceptance(actual)
        current.append(actual)
    if any(
        WakeAssignment.one(db, recipient_lookup=recipient.recipient_lookup, wire_seq=message.seq)
        for recipient, decision in zip(initial.recipients, initial.decisions, strict=True)
        if not decision.claim_decisions()
    ):
        raise IdentityConflict("no-wake observer has a conflicting unbound claim")
    return AcceptedCohort(namespace, message.seq, message.message_id, len(initial.recipients),
                          len(expected), row.accepted_at_ms, tuple(current))


def _record_optional_owner_generations(
    db: sqlite3.Connection,
    assignments: tuple[WakeAssignment, ...],
    wire_root_id: RootId,
    wire_seq: int,
) -> None:
    """Same acceptance transaction; a failed optional savepoint cannot veto N/K.

    Rows are written only while the exact cohort is unsealed. A missing or
    damaged optional schema, or a frozen old-name claim accepted after rename,
    leaves NO partial generation evidence. The reader then omits awareness.
    """
    db.execute("SAVEPOINT optional_awareness_claims")
    try:
        assert_optional_awareness_schema(db)
        for assignment in assignments:
            current = OwnerGenerations.one(db, owner_lookup=assignment.recipient_lookup)
            if current is None:
                raise IdentityConflict("optional claim generation has no current owner")
            AwarenessClaimGenerations(
                claim_id=assignment.assignment_id,
                wire_root_id=wire_root_id,
                wire_seq=wire_seq,
                owner_lookup=assignment.recipient_lookup,
                owner_generation=current.generation,
            ).insert(db)
    except (sqlite3.Error, SchemaVersionError, IdentityConflict, MalformedRecord) as error:
        db.execute("ROLLBACK TO optional_awareness_claims")
        _LOG.warning("Optional generation provenance omitted: %r", error, exc_info=error)
    finally:
        db.execute("RELEASE optional_awareness_claims")


def next_sealed_assignment(
    store: Coordination,
    recipient_lookup: ThreadLookup,
    *,
    after_seq: int = 0,
) -> WakeAssignment | None:
    """Choose pending work with a saved acknowledgement for display or execution.

    Filter pending states in SQL and read one row. Displaying work does not
    start a turn; execution still checks the process and stored message.
    """
    pending = sealed_cohort_assignments(
        store,
        recipient_lookup,
        after_seq=after_seq,
        limit=1,
        state_capability=PendingNotification,
    )
    return next(iter(pending), None)


def pending_sealed_assignments(
    store: Coordination, recipient_lookup: ThreadLookup, *, after_seq: int = 0,
) -> tuple[WakeAssignment, ...]:
    """Read acknowledged pending work once, excluding messages arriving afterward.

    Read all pages in one database transaction, continuing past completed rows
    without reviving old input. Each assignment keeps the message and decision
    recorded when it was accepted.
    """
    pending = []
    cursor = after_seq
    with store.session.read():
        while True:
            selected = sealed_cohort_assignments(
                store, recipient_lookup, after_seq=cursor, limit=100,
                state_capability=PendingNotification,
            )
            pending.extend(selected)
            if len(selected) < 100:
                return tuple(pending)
            cursor = selected[-1].wire_seq


def sealed_cohort_assignments(
    store: Coordination, recipient_lookup: ThreadLookup, *, after_seq: int = 0, limit: int = 100,
    state_capability: type = AssignmentState,
) -> tuple[WakeAssignment, ...]:
    """Bounded acknowledgement-backed projection. Never pages unbound singleton claims."""
    if (
        type(after_seq) is not int
        or after_seq < 0
        or type(limit) is not int
        or not 0 < limit <= 100
    ):
        raise ValueError("acknowledgement-backed page bounds are invalid")
    with store.session.read():
        db = store.session._connection
        CohortTable.require_installed(db)
        selected_states = AssignmentState.selection_predicate(state_capability, "c")
        return tuple(
            WakeAssignment.read(
                db.execute(
                    "SELECT c.* FROM wake_claims c "
                    "JOIN claim_batch_members m ON m.claim_id=c.assignment_id "
                    "JOIN claim_batch_receipts r ON r.wire_root_id=m.wire_root_id "
                    "AND r.wire_seq=m.wire_seq AND r.sealed=1 "
                    "JOIN cohort_delivery_receipts d ON d.wire_root_id=m.wire_root_id "
                    "AND d.wire_seq=m.wire_seq AND d.claim_id=c.assignment_id "
                    "WHERE c.recipient_lookup=? AND c.wire_seq>? "
                    f"AND {selected_states} "
                    "ORDER BY c.wire_seq,c.assignment_id LIMIT ?",
                    (recipient_lookup, after_seq, limit),
                )
            )
        )
