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
    _DDL,
    _DDL_DIGEST,
    COHORT_SCHEMA_VERSION,
    assert_optional_awareness_schema,
)
from .coordination import (
    POLICY_VERSION,
    RESOLVER_VERSION,
    IntegrityViolationError,
    SchemaVersionError,
    WakeAssignment,
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


def _assert_schema(db: sqlite3.Connection) -> None:
    try:
        version = db.execute(
            "SELECT version,ddl_digest FROM cohort_schema_meta WHERE singleton=1"
        ).fetchone()
    except sqlite3.OperationalError as error:
        raise SchemaVersionError("private cohort schema is not installed") from error
    if version is None or tuple(version) != (COHORT_SCHEMA_VERSION, _DDL_DIGEST):
        raise SchemaVersionError("unsupported private cohort schema version")
    actual = {
        row["name"]: row["sql"]
        for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger','index') "
            "AND (name LIKE 'cohort_%' OR name LIKE 'claim_batch_%')"
        )
    }
    if actual != dict(_DDL) or db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SchemaVersionError("private cohort schema is missing or drifted")


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
    row = db.execute(
        "SELECT * FROM claim_batch_receipts WHERE wire_root_id=? AND wire_seq=?",
        (initial.wire_root_id, message.seq),
    ).fetchone()
    if row is None or row["sealed"] != 1:
        raise IdentityConflict("cohort receipt is absent or unsealed")
    expected_count = sum(type(decision) is WakeDecision for decision in initial.decisions)
    if any(
        row[field] != expected
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
    expected = _expected_assignments(initial, row["accepted_at_ms"])
    members = tuple(
        db.execute(
            "SELECT ordinal,claim_id,recipient_lookup FROM claim_batch_members "
            "WHERE wire_root_id=? AND wire_seq=? ORDER BY ordinal",
            (initial.wire_root_id, message.seq),
        )
    )
    if tuple(tuple(member) for member in members) != tuple(
        (ordinal, assignment.assignment_id, assignment.recipient_lookup)
        for ordinal, assignment in enumerate(expected)
    ):
        raise IdentityConflict("sealed K claim members conflict with committed bus")
    deliveries = tuple(
        db.execute(
            "SELECT ordinal,recipient_lookup,canonical_thread,kind,claim_id "
            "FROM cohort_delivery_receipts WHERE wire_root_id=? AND wire_seq=? ORDER BY ordinal",
            (initial.wire_root_id, message.seq),
        )
    )
    selected = {assignment.recipient_lookup: assignment.assignment_id for assignment in expected}
    if tuple(tuple(delivery) for delivery in deliveries) != tuple(
        (
            ordinal,
            recipient.recipient_lookup,
            recipient.canonical_thread,
            "selected" if recipient.recipient_lookup in selected else "unmentioned_observer",
            selected.get(recipient.recipient_lookup),
        )
        for ordinal, recipient in enumerate(audience.recipients)
    ):
        raise IdentityConflict("sealed N delivery receipts conflict with committed bus")
    current: list[WakeAssignment] = []
    for accepted in expected:
        db_row = db.execute(
            "SELECT * FROM wake_claims WHERE claim_id=?", (accepted.assignment_id,)
        ).fetchone()
        if db_row is None:
            raise IdentityConflict("sealed cohort claim is missing")
        actual = _assignment(db_row)
        if not _immutable_assignment_matches(actual, accepted):
            raise IdentityConflict("sealed cohort claim immutable facts conflict")
        current.append(actual)
    # A singleton claim for an explicitly no-wake observer cannot be accepted
    # as a K member or hidden by a valid sealed N receipt.
    if any(
        db.execute(
            "SELECT 1 FROM wake_claims WHERE recipient_lookup=? AND wire_seq=?",
            (recipient.recipient_lookup, message.seq),
        ).fetchone()
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
        row["accepted_at_ms"],
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
            current = db.execute(
                "SELECT owner_thread,generation FROM owner_generations WHERE owner_lookup=?",
                (assignment.recipient_lookup,),
            ).fetchone()
            if current is None or current["owner_thread"] != assignment.recipient:
                raise IdentityConflict("optional claim generation has no current canonical owner")
            db.execute(
                "INSERT INTO awareness_claim_generations VALUES(?,?,?,?,?,?)",
                (
                    assignment.assignment_id,
                    wire_root_id,
                    wire_seq,
                    assignment.recipient_lookup,
                    assignment.recipient,
                    current["generation"],
                ),
            )
    except (sqlite3.Error, SchemaVersionError, IdentityConflict) as error:
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
    initial = bus.log.read_initial_cohort(wire_root_id, wire_seq)
    with store._transaction() as db:
        _assert_schema(db)
        receipt = db.execute(
            "SELECT sealed FROM claim_batch_receipts WHERE wire_root_id=? AND wire_seq=?",
            (wire_root_id, wire_seq),
        ).fetchone()
        if receipt is not None:
            return AlreadyApplied(_receipt_matches(db, initial))
        if db.execute("SELECT 1 FROM wake_claims WHERE wire_seq=? LIMIT 1", (wire_seq,)).fetchone():
            raise IdentityConflict("legacy singleton claim cannot be adopted by a cohort")
        if db.execute(
            "SELECT 1 FROM claim_batch_receipts WHERE wire_seq=? LIMIT 1", (wire_seq,)
        ).fetchone():
            raise IdentityConflict("wire sequence already belongs to another cohort root")
        # Frozen recipients must resolve to previously registered stable owner
        # lookups. Current aliases, tags and canonical names cannot revise N.
        for recipient in initial.audience.recipients:
            if (
                db.execute(
                    "SELECT 1 FROM participants WHERE participant_lookup=?",
                    (recipient.recipient_lookup,),
                ).fetchone()
                is None
            ):
                raise IdentityConflict("frozen recipient has no durable participant identity")
        accepted_at = store._now()
        expected = _expected_assignments(initial, accepted_at)
        db.execute(
            "INSERT INTO claim_batch_receipts (wire_root_id,wire_seq,message_id,exact_target,"
            "envelope_digest,audience_digest,decisions_digest,member_count,claim_count,"
            "manifest_codec,resolver_version,policy_version,accepted_at_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                wire_root_id,
                wire_seq,
                initial.message.message_id,
                initial.message.target,
                initial.audience.wire_envelope_digest,
                initial.audience.digest,
                initial.decisions_digest,
                len(initial.audience.recipients),
                len(expected),
                initial.manifest_codec,
                initial.resolver_version,
                initial.policy_version,
                accepted_at,
            ),
        )
        for assignment in expected:
            db.execute(
                "INSERT INTO wake_claims(claim_id,recipient,recipient_lookup,wire_seq,"
                "message_id,exact_target,audience,wake_mode,triage_verdict,disposition,"
                "resolver_version,policy_version,accepted_at_ms,updated_at_ms,"
                "revision,execution_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    assignment.assignment_id,
                    assignment.recipient,
                    assignment.recipient_lookup,
                    assignment.wire_seq,
                    assignment.message_id,
                    None,
                    assignment.audience.value,
                    assignment.lifecycle.mode.declared_name,
                    None,
                    assignment.lifecycle.declared_name,
                    assignment.resolver_version,
                    assignment.policy_version,
                    assignment.accepted_at_ms,
                    assignment.updated_at_ms,
                    1,
                    None,
                ),
            )
        for ordinal, assignment in enumerate(expected):
            db.execute(
                "INSERT INTO claim_batch_members VALUES (?,?,?,?,?)",
                (
                    wire_root_id,
                    wire_seq,
                    ordinal,
                    assignment.assignment_id,
                    assignment.recipient_lookup,
                ),
            )
        selected = {
            assignment.recipient_lookup: assignment.assignment_id for assignment in expected
        }
        for ordinal, recipient in enumerate(initial.audience.recipients):
            assignment_id = selected.get(recipient.recipient_lookup)
            db.execute(
                "INSERT INTO cohort_delivery_receipts VALUES (?,?,?,?,?,?,?)",
                (
                    wire_root_id,
                    wire_seq,
                    ordinal,
                    recipient.recipient_lookup,
                    recipient.canonical_thread,
                    "selected" if assignment_id is not None else "unmentioned_observer",
                    assignment_id,
                ),
            )
        _record_optional_owner_generations(db, expected, wire_root_id, wire_seq)
        db.execute(
            "UPDATE claim_batch_receipts SET sealed=1 WHERE wire_root_id=? AND wire_seq=?",
            (wire_root_id, wire_seq),
        )
        return Applied(_receipt_matches(db, initial))


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
        _assert_schema(db)
        return tuple(
            _assignment(row)
            for row in db.execute(
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
