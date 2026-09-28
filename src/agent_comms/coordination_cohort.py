"""Opt-in committed-bus to ONE-transaction full-N coordinator acceptance.

No live wake, context assertion, publication, retry, or cursor advancement.
Only a MessageBus-owned original raw-row read can enter this writer. A frozen
pure digest, caller-supplied DTO, or legacy singleton claim is not bus proof.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field

from .bus_publication import CommittedInitial
from .cohort_schema import (
    AwarenessClaimGenerations,
    ClaimBatchMembers,
    ClaimBatchReceipts,
    CohortDeliveryReceipts,
    assert_cohort_schema,
    assert_optional_awareness_schema,
)
from .coordination import (
    POLICY_VERSION,
    RESOLVER_VERSION,
    IntegrityViolationError,
    OwnerGenerations,
    Participants,
    SchemaVersionError,
    WakeAssignment,
    WakeClaims,
)
from .coordination_store import (
    AlreadyApplied,
    Applied,
    IdentityConflict,
    MutationStore,
    _assignment,
)
from .message_bus import MessageBus
from .wake import NoWakeDecision, WakeDecision

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AcceptedCohort:
    wire_root_id: str
    wire_seq: int
    message_id: str
    member_count: int
    assignment_count: int = field(metadata={"wire_name": "claim_count"})
    accepted_at_ms: int
    assignments: tuple[WakeAssignment, ...] = field(metadata={"wire_name": "claims"})


def _assignment_id(root: str, seq: int, lookup: str) -> str:
    identity = f"agent-comms:cohort-claim:v1\0{root}:{seq}:{lookup}".encode()
    return "cohort-v1:" + hashlib.sha256(identity).hexdigest()


def _expected_assignments(
    initial: CommittedInitial, accepted_at_ms: int
) -> tuple[WakeAssignment, ...]:
    result: list[WakeAssignment] = []
    for recipient, decision in zip(initial.audience.recipients, initial.decisions, strict=True):
        if recipient.recipient_lookup != decision.recipient:
            raise IntegrityViolationError("bus decision does not match frozen N")
        if type(decision) is NoWakeDecision:
            continue
        if type(decision) is not WakeDecision:
            raise IntegrityViolationError("bus decision has an unsupported kind")
        result.append(
            WakeAssignment(
                assignment_id=_assignment_id(
                    initial.wire_root_id, initial.message.seq, decision.recipient
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
                lifecycle=decision.wake_mode.initial_state()(),
            )
        )
    return tuple(result)


def _immutable_assignment_matches(current: WakeAssignment, expected: WakeAssignment) -> bool:
    return current.lifecycle.mode == expected.lifecycle.mode and all(
        getattr(current, field) == getattr(expected, field)
        for field in (
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
    )


def _receipt_matches(db: sqlite3.Connection, initial: CommittedInitial) -> AcceptedCohort:
    message = initial.message
    audience = initial.audience
    row = ClaimBatchReceipts.one(db, wire_root_id=initial.wire_root_id, wire_seq=message.seq)
    if row is None or not row.sealed:
        raise IdentityConflict("cohort receipt is absent or unsealed")
    expected_count = sum(type(decision) is WakeDecision for decision in initial.decisions)
    if any(
        getattr(row, field) != expected
        for field, expected in (
            ("message_id", message.message_id),
            ("exact_target", message.target),
            ("envelope_digest", audience.wire_envelope_digest),
            ("audience_digest", audience.digest),
            ("decisions_digest", initial.decisions_digest),
            ("member_count", len(audience.recipients)),
            ("claim_count", expected_count),
            ("manifest_codec", initial.manifest_codec),
            ("resolver_version", initial.resolver_version),
            ("policy_version", initial.policy_version),
        )
    ):
        raise IdentityConflict("sealed cohort receipt conflicts with committed bus")
    expected = _expected_assignments(initial, row.accepted_at_ms)
    members = ClaimBatchMembers.select(
        db,
        where="wire_root_id=? AND wire_seq=?",
        parameters=(initial.wire_root_id, message.seq),
        order_by=("ordinal",),
    )
    if members != [
        ClaimBatchMembers(
            wire_root_id=initial.wire_root_id,
            wire_seq=message.seq,
            ordinal=ordinal,
            claim_id=assignment.assignment_id,
            recipient_lookup=assignment.recipient_lookup,
        )
        for ordinal, assignment in enumerate(expected)
    ]:
        raise IdentityConflict("sealed K claim members conflict with committed bus")
    deliveries = CohortDeliveryReceipts.select(
        db,
        where="wire_root_id=? AND wire_seq=?",
        parameters=(initial.wire_root_id, message.seq),
        order_by=("ordinal",),
    )
    selected = {assignment.recipient_lookup: assignment.assignment_id for assignment in expected}
    if deliveries != [
        CohortDeliveryReceipts(
            wire_root_id=initial.wire_root_id,
            wire_seq=message.seq,
            ordinal=ordinal,
            recipient_lookup=recipient.recipient_lookup,
            canonical_thread=recipient.canonical_thread,
            kind="selected" if recipient.recipient_lookup in selected else "unmentioned_observer",
            claim_id=selected.get(recipient.recipient_lookup),
        )
        for ordinal, recipient in enumerate(audience.recipients)
    ]:
        raise IdentityConflict("sealed N delivery receipts conflict with committed bus")
    current: list[WakeAssignment] = []
    for accepted in expected:
        db_row = WakeClaims.one(db, claim_id=accepted.assignment_id)
        if db_row is None:
            raise IdentityConflict("sealed cohort claim is missing")
        actual = _assignment(db_row)
        if not _immutable_assignment_matches(actual, accepted):
            raise IdentityConflict("sealed cohort claim immutable facts conflict")
        current.append(actual)
    # A singleton claim for an explicitly no-wake observer cannot be accepted
    # as a K member or hidden by a valid sealed N receipt.
    if any(
        WakeClaims.one(db, recipient_lookup=recipient.recipient_lookup, wire_seq=message.seq)
        for recipient, decision in zip(audience.recipients, initial.decisions, strict=True)
        if type(decision) is NoWakeDecision
    ):
        raise IdentityConflict("no-wake observer has a conflicting legacy claim")
    return AcceptedCohort(
        initial.wire_root_id,
        message.seq,
        message.message_id,
        len(audience.recipients),
        len(expected),
        row.accepted_at_ms,
        tuple(current),
    )


def _record_optional_owner_generations(
    db: sqlite3.Connection,
    assignments: tuple[WakeAssignment, ...],
    wire_root_id: str,
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
            if current is None or current.owner_thread != assignment.recipient:
                raise IdentityConflict("optional claim generation has no current canonical owner")
            AwarenessClaimGenerations(
                claim_id=assignment.assignment_id,
                wire_root_id=wire_root_id,
                wire_seq=wire_seq,
                recipient_lookup=assignment.recipient_lookup,
                canonical_thread=assignment.recipient,
                owner_generation=current.generation,
            ).insert(db)
    except (sqlite3.Error, SchemaVersionError, IdentityConflict, ValueError, TypeError) as error:
        db.execute("ROLLBACK TO optional_awareness_claims")
        _LOG.warning("Optional generation provenance omitted (%s)", type(error).__name__)
    finally:
        db.execute("RELEASE optional_awareness_claims")


def accept_initial_cohort(
    bus: MessageBus,
    wire_root_id: str,
    wire_seq: int,
    store: MutationStore,
) -> Applied[AcceptedCohort] | AlreadyApplied[AcceptedCohort]:
    """Bus-read first, then one SQLite transaction for every N/K fact.

    Participants are separately durable identities; this cannot manufacture a
    participant owner or commit one based merely on a bus message. If the
    coordinator has not registered each frozen stable lookup, no N is accepted.
    """
    if type(bus) is not MessageBus or type(store) is not MutationStore:
        raise TypeError("cohort acceptance requires the actual bus and coordinator stores")
    with bus.log.locked():
        marker = bus.log._private_marker_unlocked()
        if marker.root_id != wire_root_id:
            raise IdentityConflict("cohort admission wire root changed")
        if wire_seq <= marker.admission_after_seq:
            raise IdentityConflict("historical source precedes the current admission floor")
    initial = bus.log.read_initial_cohort(wire_root_id, wire_seq)
    with store._transaction() as db:
        assert_cohort_schema(db)
        receipt = ClaimBatchReceipts.one(db, wire_root_id=wire_root_id, wire_seq=wire_seq)
        if receipt is not None:
            return AlreadyApplied(_receipt_matches(db, initial))
        if WakeClaims.select(db, where="wire_seq=?", parameters=(wire_seq,)):
            raise IdentityConflict("singleton claim cannot be adopted by a cohort")
        if ClaimBatchReceipts.select(db, where="wire_seq=?", parameters=(wire_seq,)):
            raise IdentityConflict("wire sequence already belongs to another cohort root")
        for recipient in initial.audience.recipients:
            if Participants.one(db, participant_lookup=recipient.recipient_lookup) is None:
                raise IdentityConflict("frozen recipient has no durable participant identity")
        accepted_at = store._now()
        expected = _expected_assignments(initial, accepted_at)
        ClaimBatchReceipts(
            wire_root_id=wire_root_id,
            wire_seq=wire_seq,
            message_id=initial.message.message_id,
            exact_target=initial.message.target,
            envelope_digest=initial.audience.wire_envelope_digest,
            audience_digest=initial.audience.digest,
            decisions_digest=initial.decisions_digest,
            member_count=len(initial.audience.recipients),
            claim_count=len(expected),
            manifest_codec=initial.manifest_codec,
            resolver_version=initial.resolver_version,
            policy_version=initial.policy_version,
            accepted_at_ms=accepted_at,
        ).insert(db)
        for ordinal, assignment in enumerate(expected):
            WakeClaims(
                claim_id=assignment.assignment_id,
                recipient=assignment.recipient,
                recipient_lookup=assignment.recipient_lookup,
                wire_seq=assignment.wire_seq,
                message_id=assignment.message_id,
                exact_target=None,
                audience=assignment.audience.value,
                wake_mode=assignment.lifecycle.mode.declared_name,
                triage_verdict=None,
                disposition=assignment.lifecycle.declared_name,
                resolver_version=assignment.resolver_version,
                policy_version=assignment.policy_version,
                accepted_at_ms=assignment.accepted_at_ms,
                updated_at_ms=assignment.updated_at_ms,
                revision=1,
                execution_id=None,
            ).insert(db)
            ClaimBatchMembers(
                wire_root_id=wire_root_id,
                wire_seq=wire_seq,
                ordinal=ordinal,
                claim_id=assignment.assignment_id,
                recipient_lookup=assignment.recipient_lookup,
            ).insert(db)
        selected = {
            assignment.recipient_lookup: assignment.assignment_id for assignment in expected
        }
        for ordinal, recipient in enumerate(initial.audience.recipients):
            assignment_id = selected.get(recipient.recipient_lookup)
            CohortDeliveryReceipts(
                wire_root_id=wire_root_id,
                wire_seq=wire_seq,
                ordinal=ordinal,
                recipient_lookup=recipient.recipient_lookup,
                canonical_thread=recipient.canonical_thread,
                kind="selected" if assignment_id is not None else "unmentioned_observer",
                claim_id=assignment_id,
            ).insert(db)
        _record_optional_owner_generations(db, expected, wire_root_id, wire_seq)
        ClaimBatchReceipts.update(
            db,
            where="wire_root_id=? AND wire_seq=?",
            parameters=(wire_root_id, wire_seq),
            sealed=True,
        )
        return Applied(_receipt_matches(db, initial))


def sealed_cohort_sequences(store: MutationStore, wire_root_id: str) -> frozenset[int]:
    """Already committed cohort batches, derived from their immutable receipts."""
    with store._read_transaction():
        assert_cohort_schema(store._connection)
        return frozenset(
            row.wire_seq
            for row in ClaimBatchReceipts.select(
                store._connection,
                where="wire_root_id=? AND sealed=1",
                parameters=(wire_root_id,),
            )
        )


def next_sealed_assignment(
    store: MutationStore,
    recipient_lookup: str,
    owner_name: str,
    *,
    after_seq: int = 0,
) -> WakeAssignment | None:
    """One receipt-backed pending selection shared by observation and execution.

    Saturated settled pages cannot hide later work. Observation grants no turn;
    the execution still checks live ownership and verifies its original source.
    """
    cursor = after_seq
    for _ in range(10):
        selected = sealed_cohort_assignments(store, recipient_lookup, after_seq=cursor, limit=100)
        for assignment in selected:
            if assignment.recipient == owner_name and (
                assignment.lifecycle.triage_pending or assignment.lifecycle.full_pending
            ):
                return assignment
        if len(selected) < 100:
            return None
        cursor = selected[-1].wire_seq
    raise IdentityConflict(f"sealed claim scan exhausted; retry explicitly with after_seq={cursor}")


def sealed_cohort_assignments(
    store: MutationStore, recipient_lookup: str, *, after_seq: int = 0, limit: int = 100
) -> tuple[WakeAssignment, ...]:
    """Bounded receipt-backed projection. Never pages legacy singleton claims."""
    if (
        type(after_seq) is not int
        or after_seq < 0
        or type(limit) is not int
        or not 0 < limit <= 100
    ):
        raise ValueError("receipt-backed page bounds are invalid")
    with store._read_transaction():
        db = store._connection
        assert_cohort_schema(db)
        return tuple(
            _assignment(row)
            for row in WakeClaims.read(
                db.execute(
                    "SELECT c.* FROM wake_claims c "
                    "JOIN claim_batch_members m ON m.claim_id=c.claim_id "
                    "JOIN claim_batch_receipts r ON r.wire_root_id=m.wire_root_id "
                    "AND r.wire_seq=m.wire_seq AND r.sealed=1 "
                    "JOIN cohort_delivery_receipts d ON d.wire_root_id=m.wire_root_id "
                    "AND d.wire_seq=m.wire_seq AND d.claim_id=c.claim_id AND d.kind='selected' "
                    "WHERE c.recipient_lookup=? AND c.wire_seq>? "
                    "ORDER BY c.wire_seq,c.claim_id LIMIT ?",
                    (recipient_lookup, after_seq, limit),
                )
            )
        )
