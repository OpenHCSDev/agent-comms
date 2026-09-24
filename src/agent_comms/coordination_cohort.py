"""Opt-in committed-bus to ONE-transaction full-N coordinator acceptance.

No live wake, context assertion, publication, retry, or cursor advancement.
Only a MessageBus-owned original raw-row read can enter this writer. A frozen
pure digest, caller-supplied DTO, or legacy singleton claim is not bus proof.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from .bus_publication import CommittedInitial
from .cohort_schema import _DDL, _DDL_DIGEST, COHORT_SCHEMA_VERSION
from .coordination import (
    POLICY_VERSION,
    RESOLVER_VERSION,
    ClaimDisposition,
    IntegrityViolationError,
    SchemaVersionError,
    WakeClaim,
    WakeMode,
)
from .coordination_store import AlreadyApplied, Applied, IdentityConflict, MutationStore, _claim
from .declarations import MessageBus
from .wake import NoWakeDecision, WakeDecision


@dataclass(frozen=True, slots=True)
class AcceptedCohort:
    wire_root_id: str
    wire_seq: int
    message_id: str
    member_count: int
    claim_count: int
    accepted_at_ms: int
    claims: tuple[WakeClaim, ...]


def _claim_id(root: str, seq: int, lookup: str) -> str:
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


def _expected_claims(initial: CommittedInitial, accepted_at_ms: int) -> tuple[WakeClaim, ...]:
    result: list[WakeClaim] = []
    for recipient, decision in zip(initial.audience.recipients, initial.decisions, strict=True):
        if recipient.recipient_lookup != decision.recipient:
            raise IntegrityViolationError("bus decision does not match frozen N")
        if type(decision) is NoWakeDecision:
            continue
        if type(decision) is not WakeDecision:
            raise IntegrityViolationError("bus decision has an unsupported kind")
        result.append(
            WakeClaim(
                claim_id=_claim_id(initial.wire_root_id, initial.message.seq, decision.recipient),
                recipient=recipient.canonical_thread,
                recipient_lookup=decision.recipient,
                wire_seq=initial.message.seq,
                message_id=initial.message.message_id,
                exact_target=None,
                audience=decision.audience,
                wake_mode=decision.wake_mode,
                triage_verdict=None,
                disposition={
                    WakeMode.PASSIVE: ClaimDisposition.PASSIVE,
                    WakeMode.BOUNDED_TRIAGE: ClaimDisposition.TRIAGE_PENDING,
                    WakeMode.FULL: ClaimDisposition.FULL_PENDING,
                }[decision.wake_mode],
                accepted_at_ms=accepted_at_ms,
                updated_at_ms=accepted_at_ms,
                revision=1,
                resolver_version=RESOLVER_VERSION,
                policy_version=POLICY_VERSION,
            )
        )
    return tuple(result)


def _immutable_claim_matches(current: WakeClaim, expected: WakeClaim) -> bool:
    return all(
        getattr(current, field) == getattr(expected, field)
        for field in (
            "claim_id",
            "recipient",
            "recipient_lookup",
            "wire_seq",
            "message_id",
            "audience",
            "wake_mode",
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
    expected = _expected_claims(initial, row["accepted_at_ms"])
    members = tuple(
        db.execute(
            "SELECT ordinal,claim_id,recipient_lookup FROM claim_batch_members "
            "WHERE wire_root_id=? AND wire_seq=? ORDER BY ordinal",
            (initial.wire_root_id, message.seq),
        )
    )
    if tuple(tuple(member) for member in members) != tuple(
        (ordinal, claim.claim_id, claim.recipient_lookup) for ordinal, claim in enumerate(expected)
    ):
        raise IdentityConflict("sealed K claim members conflict with committed bus")
    deliveries = tuple(
        db.execute(
            "SELECT ordinal,recipient_lookup,canonical_thread,kind,claim_id "
            "FROM cohort_delivery_receipts WHERE wire_root_id=? AND wire_seq=? ORDER BY ordinal",
            (initial.wire_root_id, message.seq),
        )
    )
    selected = {claim.recipient_lookup: claim.claim_id for claim in expected}
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
    current: list[WakeClaim] = []
    for accepted in expected:
        db_row = db.execute(
            "SELECT * FROM wake_claims WHERE claim_id=?", (accepted.claim_id,)
        ).fetchone()
        if db_row is None:
            raise IdentityConflict("sealed cohort claim is missing")
        actual = _claim(db_row)
        if not _immutable_claim_matches(actual, accepted):
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
    initial = bus.read_initial_cohort(wire_root_id, wire_seq)
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
        expected = _expected_claims(initial, accepted_at)
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
        for claim in expected:
            db.execute(
                "INSERT INTO wake_claims(claim_id,recipient,recipient_lookup,wire_seq,"
                "message_id,exact_target,audience,wake_mode,triage_verdict,disposition,"
                "resolver_version,policy_version,accepted_at_ms,updated_at_ms,"
                "revision,execution_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    claim.claim_id,
                    claim.recipient,
                    claim.recipient_lookup,
                    claim.wire_seq,
                    claim.message_id,
                    None,
                    claim.audience.value,
                    claim.wake_mode.value,
                    None,
                    claim.disposition.value,
                    claim.resolver_version,
                    claim.policy_version,
                    claim.accepted_at_ms,
                    claim.updated_at_ms,
                    1,
                    None,
                ),
            )
        for ordinal, claim in enumerate(expected):
            db.execute(
                "INSERT INTO claim_batch_members VALUES (?,?,?,?,?)",
                (wire_root_id, wire_seq, ordinal, claim.claim_id, claim.recipient_lookup),
            )
        selected = {claim.recipient_lookup: claim.claim_id for claim in expected}
        for ordinal, recipient in enumerate(initial.audience.recipients):
            claim_id = selected.get(recipient.recipient_lookup)
            db.execute(
                "INSERT INTO cohort_delivery_receipts VALUES (?,?,?,?,?,?,?)",
                (
                    wire_root_id,
                    wire_seq,
                    ordinal,
                    recipient.recipient_lookup,
                    recipient.canonical_thread,
                    "selected" if claim_id is not None else "unmentioned_observer",
                    claim_id,
                ),
            )
        db.execute(
            "UPDATE claim_batch_receipts SET sealed=1 WHERE wire_root_id=? AND wire_seq=?",
            (wire_root_id, wire_seq),
        )
        return Applied(_receipt_matches(db, initial))


def sealed_cohort_claims(
    store: MutationStore, recipient_lookup: str, *, after_seq: int = 0, limit: int = 100
) -> tuple[WakeClaim, ...]:
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
            _claim(row)
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
