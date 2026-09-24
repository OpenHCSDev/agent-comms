"""Transactional, nonpublication mutations for the private coordination schema.

This module does not grant bus, runtime, ACP or publication authority.  In
particular a frozen PUBLISHING intent cannot be resolved by this store.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Generic, TypeVar

from .coordination import (
    ACTIVE_ATTEMPT_PHASES,
    ATTEMPT_PHASE_TRANSITIONS,
    CLAIM_DISPOSITION_TRANSITIONS,
    MAX_IDENTIFIER_CHARS,
    MAX_REASON_CODE_CHARS,
    MAX_SANITIZED_DETAIL_CHARS,
    POLICY_VERSION,
    RESOLVER_VERSION,
    ACPClientConnectivity,
    AttemptPhase,
    AttemptRecord,
    ClaimDisposition,
    ConnectivityFacet,
    CoordinationError,
    CoordinationStore,
    CurrentExecutionPointer,
    ExecutionClaimLink,
    ExecutionOrigin,
    ExecutionRecord,
    ExecutionStatus,
    IntegrityViolationError,
    MessageAudience,
    ObligationState,
    OwnerConnectivity,
    OwnerFence,
    PublicationIntent,
    PublicationReceipt,
    RecoveryAudit,
    RecoveryKind,
    RecoverySnapshot,
    ReplayAssessment,
    ReplayFact,
    ResponseObligation,
    TriageVerdict,
    WakeClaim,
    WakeMode,
    retry_disposition_authorized,
)
from .declarations import MessageType

T = TypeVar("T")
INITIAL_LEASE_POLICY_VERSION = "initial-lease-v1"
INITIAL_LEASE_DURATION_MS = 60_000
MAX_LEASE_RENEWAL_MS = 300_000


class StaleRevision(CoordinationError):  # noqa: N818 - nominal outcome name
    """The supplied CAS revision is not the authoritative revision."""


class StaleFence(CoordinationError):  # noqa: N818 - nominal outcome name
    """The owner, generation, pointer, token or attempt is no longer current."""


class IdentityConflict(CoordinationError):  # noqa: N818 - nominal outcome name
    """An immutable natural identity is already bound to different facts."""


class RecoveryBlocked(CoordinationError):  # noqa: N818 - nominal outcome name
    """A dead attempt lacks authoritative backend-final evidence."""


class PublicationUncertain(CoordinationError):  # noqa: N818 - nominal outcome name
    """A frozen publishing intent has no authoritative bus-keyed receipt."""


class PublicationActivationBlocked(CoordinationError):  # noqa: N818 - nominal outcome name
    """No bus-owned idempotent append receipt is available in this slice."""


@dataclass(frozen=True, slots=True)
class Applied(Generic[T]):
    value: T


@dataclass(frozen=True, slots=True)
class AlreadyApplied(Generic[T]):
    value: T


@dataclass(frozen=True, slots=True)
class ParticipantSnapshot:
    lookup: str
    display_name: str
    committed: bool
    aliases: tuple[str, ...]
    owner_thread: str
    generation: int
    pointer: CurrentExecutionPointer


@dataclass(frozen=True, slots=True)
class StartResult:
    snapshot: RecoverySnapshot
    fence: OwnerFence


@dataclass(frozen=True, slots=True, init=False)
class VerifiedOwnerLoss:
    """Future verifier-minted, identity-bound registry-owner loss attestation.

    There is NO authenticated Registry/PID verifier within Slice 2.  Normal
    construction and production verification remain disabled until separately
    authorized runtime integration.  A caller's boolean or forged instance is
    not evidence of old registry-owner loss.
    """

    execution_id: str
    owner_lookup: str
    owner_generation: int
    attempt_ordinal: int

    def __init__(self, **_unsupported: object) -> None:
        raise RecoveryBlocked("owner-loss verifier is not activated")


def _owner_loss_verified(
    _proof: VerifiedOwnerLoss | None,
    _execution_id: str,
    _owner_lookup: str,
    _owner_generation: int,
    _attempt_ordinal: int,
) -> bool:
    """Activation-blocked issuer check; a future trusted verifier must own issuance."""
    return False


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitorEvidence:
    """Observed Pi RPC child exit and backend finality, NOT owner-loss proof."""

    subprocess_dead: bool
    backend_done: bool
    reason_code: str
    observed_at_ms: int
    unknown_effects: bool = True

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (self.subprocess_dead, self.backend_done, self.unknown_effects)
        ):
            raise ValueError("monitor observations must be explicit booleans")
        if not 1 <= len(self.reason_code) <= MAX_REASON_CODE_CHARS:
            raise ValueError("monitor reason must be bounded and nonempty")
        if self.observed_at_ms < 0:
            raise ValueError("negative evidence timestamp")


def prepare_fence_token() -> str:
    """Deliver the CSPRNG secret to its owner *before* any database transaction."""
    return secrets.token_hex(32)


def _digest(token: str) -> str:
    if not isinstance(token, str) or not 1 <= len(token) <= MAX_IDENTIFIER_CHARS:
        raise ValueError("invalid retained fence token")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bounded_reason(reason: str | None) -> None:
    if reason is not None and not 1 <= len(reason) <= MAX_REASON_CODE_CHARS:
        raise ValueError("reason code is invalid")


def _claim(row: sqlite3.Row) -> WakeClaim:
    return WakeClaim(
        claim_id=row["claim_id"],
        recipient=row["recipient"],
        recipient_lookup=row["recipient_lookup"],
        wire_seq=row["wire_seq"],
        message_id=row["message_id"],
        exact_target=row["exact_target"],
        audience=MessageAudience(row["audience"]),
        wake_mode=WakeMode(row["wake_mode"]),
        triage_verdict=(TriageVerdict(row["triage_verdict"]) if row["triage_verdict"] else None),
        disposition=ClaimDisposition(row["disposition"]),
        accepted_at_ms=row["accepted_at_ms"],
        updated_at_ms=row["updated_at_ms"],
        revision=row["revision"],
        resolver_version=row["resolver_version"],
        policy_version=row["policy_version"],
        execution_id=row["execution_id"],
    )


def _execution(row: sqlite3.Row) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=row["execution_id"],
        origin=ExecutionOrigin(row["origin"]),
        status=ExecutionStatus(row["status"]),
        owner_thread=row["owner_thread"],
        owner_lookup=row["owner_lookup"],
        revision=row["revision"],
        current_attempt_ordinal=row["current_attempt_ordinal"],
        max_attempts=row["max_attempts"],
        reason_code=row["reason_code"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
        exact_target=row["exact_target"],
    )


def _attempt(row: sqlite3.Row) -> AttemptRecord:
    return AttemptRecord(
        execution_id=row["execution_id"],
        attempt_ordinal=row["attempt_ordinal"],
        owner_lookup=row["owner_lookup"],
        owner_thread=row["owner_thread"],
        owner_generation=row["owner_generation"],
        owner_token_digest=row["owner_token_digest"],
        phase=AttemptPhase(row["phase"]),
        revision=row["revision"],
        lease_expires_at_ms=row["lease_expires_at_ms"],
        last_progress_at_ms=row["last_progress_at_ms"],
        backend_done=bool(row["backend_done"]),
        process_dead=bool(row["process_dead"]),
        reason_code=row["reason_code"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


class MutationStore(CoordinationStore):
    """Single-writer transactions over Slice-1's frozen private schema."""

    def __init__(self, path: str, *, clock_ms: Callable[[], int] | None = None) -> None:
        super().__init__(path)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def _now(self, floor: int = 0) -> int:
        now = self._clock_ms()
        if type(now) is not int or now < 0:
            raise ValueError("coordinator clock must return a nonnegative integer millisecond")
        return max(now, floor)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._connection
        if db.in_transaction:
            raise IntegrityViolationError("nested coordinator transaction is not allowed")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        """Project one committed database state, or reuse an enclosing write transaction."""
        db = self._connection
        if db.in_transaction:
            yield
            return
        db.execute("BEGIN")
        try:
            yield
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")

    def _row(self, table: str, column: str, key: object) -> sqlite3.Row | None:
        # Both identifiers are exclusively internal static call sites.
        row: sqlite3.Row | None = self._connection.execute(
            f"SELECT * FROM {table} WHERE {column} = ?", (key,)
        ).fetchone()
        return row

    def _participant(self, lookup: str) -> ParticipantSnapshot:
        db = self._connection
        person = self._row("participants", "participant_lookup", lookup)
        generation = self._row("owner_generations", "owner_lookup", lookup)
        pointer = self._row("current_executions", "owner_lookup", lookup)
        if person is None or generation is None or pointer is None:
            raise IdentityConflict("participant aggregate is not registered")
        aliases = tuple(
            row[0]
            for row in db.execute(
                "SELECT alias FROM participant_aliases WHERE participant_lookup=? "
                "ORDER BY renamed_at_ms, rowid",
                (lookup,),
            )
        )
        return ParticipantSnapshot(
            lookup,
            person["display_name"],
            bool(person["committed"]),
            aliases,
            generation["owner_thread"],
            generation["generation"],
            CurrentExecutionPointer(
                lookup,
                pointer["execution_id"],
                pointer["attempt_ordinal"],
                pointer["pointer_revision"],
            ),
        )

    def participant(self, lookup: str) -> ParticipantSnapshot:
        with self._read_transaction():
            return self._participant(lookup)

    def register_participant(
        self,
        lookup: str,
        display_name: str,
        owner_thread: str,
        *,
        alias: str | None = None,
        committed: bool = False,
    ) -> Applied[ParticipantSnapshot] | AlreadyApplied[ParticipantSnapshot]:
        if type(committed) is not bool:
            raise ValueError("committed must be a boolean")
        for value in (lookup, display_name, owner_thread, alias or lookup):
            if not isinstance(value, str) or not 1 <= len(value) <= MAX_IDENTIFIER_CHARS:
                raise ValueError("participant identity must be bounded and nonempty")
        primary = alias or lookup
        with self._transaction() as db:
            existing = self._row("participants", "participant_lookup", lookup)
            if existing is not None:
                snapshot = self._participant(lookup)
                # Commitment, display name and generation owner may advance.
                # The lookup and first accepted alias are the durable identity.
                # No mutable registration-row bundle becomes a fingerprint.
                if not snapshot.aliases or snapshot.aliases[0] != primary:
                    raise IdentityConflict("participant registration alias conflicts")
                return AlreadyApplied(snapshot)
            if self._row("participant_aliases", "alias", primary) is not None:
                raise IdentityConflict("alias already belongs to another participant")
            db.execute(
                "INSERT INTO participants VALUES (?,?,?)", (lookup, display_name, int(committed))
            )
            db.execute(
                "INSERT INTO participant_aliases VALUES (?,?,?)", (primary, lookup, self._now())
            )
            db.execute("INSERT INTO owner_generations VALUES (?,?,1)", (lookup, owner_thread))
            db.execute(
                "INSERT INTO current_executions"
                "(owner_lookup,execution_id,attempt_ordinal,pointer_revision) "
                "VALUES (?,NULL,NULL,0)",
                (lookup,),
            )
            return Applied(self._participant(lookup))

    def commit_participant(
        self, lookup: str
    ) -> Applied[ParticipantSnapshot] | AlreadyApplied[ParticipantSnapshot]:
        with self._transaction() as db:
            person = self._participant(lookup)
            if person.committed:
                return AlreadyApplied(person)
            db.execute(
                "UPDATE participants SET committed=1 WHERE participant_lookup=? AND committed=0",
                (lookup,),
            )
            return Applied(self._participant(lookup))

    def rename_participant(
        self,
        lookup: str,
        alias: str,
        display_name: str,
        *,
        expected_generation: int,
    ) -> Applied[ParticipantSnapshot] | AlreadyApplied[ParticipantSnapshot]:
        for value in (alias, display_name):
            if not isinstance(value, str) or not 1 <= len(value) <= MAX_IDENTIFIER_CHARS:
                raise ValueError("alias and display name must be bounded")
        with self._transaction() as db:
            person = self._participant(lookup)
            if person.generation != expected_generation:
                raise StaleRevision("owner generation changed")
            existing = self._row("participant_aliases", "alias", alias)
            if existing is not None:
                if existing["participant_lookup"] != lookup:
                    raise IdentityConflict("alias is already assigned")
                return AlreadyApplied(person)
            last_alias_ms = db.execute(
                "SELECT max(renamed_at_ms) FROM participant_aliases WHERE participant_lookup=?",
                (lookup,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO participant_aliases VALUES (?,?,?)",
                (alias, lookup, self._now(last_alias_ms)),
            )
            db.execute(
                "UPDATE participants SET display_name=? WHERE participant_lookup=?",
                (display_name, lookup),
            )
            return Applied(self._participant(lookup))

    def advance_owner_generation(
        self,
        lookup: str,
        owner_thread: str,
        *,
        expected_generation: int,
    ) -> Applied[ParticipantSnapshot]:
        if not isinstance(owner_thread, str) or not 1 <= len(owner_thread) <= MAX_IDENTIFIER_CHARS:
            raise ValueError("owner thread must be bounded and nonempty")
        with self._transaction() as db:
            person = self._participant(lookup)
            if not person.committed:
                raise IdentityConflict("uncommitted participant cannot own a generation")
            if person.generation != expected_generation:
                raise StaleRevision("owner generation changed")
            db.execute(
                "UPDATE owner_generations SET owner_thread=?,generation=generation+1 "
                "WHERE owner_lookup=? AND generation=?",
                (owner_thread, lookup, expected_generation),
            )
            return Applied(self._participant(lookup))

    def claim(self, claim_id: str) -> WakeClaim:
        row = self._row("wake_claims", "claim_id", claim_id)
        if row is None:
            raise IdentityConflict("unknown claim")
        return _claim(row)

    def accept_claim(self, claim: WakeClaim) -> Applied[WakeClaim] | AlreadyApplied[WakeClaim]:
        if (
            claim.revision != 1
            or claim.execution_id is not None
            or claim.exact_target is not None
            or claim.updated_at_ms != claim.accepted_at_ms
            or claim.triage_verdict is not None
            or claim.disposition
            is not {
                WakeMode.PASSIVE: ClaimDisposition.PASSIVE,
                WakeMode.BOUNDED_TRIAGE: ClaimDisposition.TRIAGE_PENDING,
                WakeMode.FULL: ClaimDisposition.FULL_PENDING,
            }[claim.wake_mode]
            or claim.resolver_version != RESOLVER_VERSION
            or claim.policy_version != POLICY_VERSION
        ):
            raise IdentityConflict("claim acceptance requires initial frozen decision")
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM wake_claims WHERE claim_id=? OR (recipient_lookup=? AND wire_seq=?)",
                (claim.claim_id, claim.recipient_lookup, claim.wire_seq),
            ).fetchone()
            if row is not None:
                current = _claim(row)
                immutable = (
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
                if any(getattr(current, name) != getattr(claim, name) for name in immutable):
                    raise IdentityConflict("accepted claim identity conflicts")
                return AlreadyApplied(current)
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
                    claim.triage_verdict.value if claim.triage_verdict else None,
                    claim.disposition.value,
                    claim.resolver_version,
                    claim.policy_version,
                    claim.accepted_at_ms,
                    claim.updated_at_ms,
                    1,
                    None,
                ),
            )
            return Applied(claim)

    def transition_preengagement(
        self,
        claim_id: str,
        disposition: ClaimDisposition,
        *,
        expected_revision: int,
        verdict: TriageVerdict | None = None,
    ) -> Applied[WakeClaim]:
        disposition = ClaimDisposition(disposition)
        with self._transaction() as db:
            current = self.claim(claim_id)
            if current.revision != expected_revision:
                raise StaleRevision("claim revision changed")
            if (
                current.execution_id is not None
                or disposition
                not in {
                    ClaimDisposition.IGNORED,
                    ClaimDisposition.DEFERRED,
                    ClaimDisposition.FAILED,
                    ClaimDisposition.TRIAGE_PENDING,
                    ClaimDisposition.FULL_PENDING,
                }
                or disposition not in CLAIM_DISPOSITION_TRANSITIONS[current.disposition]
            ):
                raise IdentityConflict("preengagement transition is not declared")
            after = replace(
                current,
                disposition=disposition,
                triage_verdict=verdict,
                updated_at_ms=self._now(current.updated_at_ms),
                revision=current.revision + 1,
            )
            db.execute(
                "UPDATE wake_claims SET disposition=?,triage_verdict=?,updated_at_ms=?,"
                "revision=? WHERE claim_id=? AND revision=?",
                (
                    disposition.value,
                    verdict.value if verdict else None,
                    after.updated_at_ms,
                    after.revision,
                    claim_id,
                    expected_revision,
                ),
            )
            return Applied(after)

    def snapshot(self, execution_id: str) -> RecoverySnapshot:
        with self._read_transaction():
            return self._snapshot(execution_id)

    def _snapshot(self, execution_id: str) -> RecoverySnapshot:
        db = self._connection
        row = self._row("executions", "execution_id", execution_id)
        if row is None:
            raise IdentityConflict("unknown execution")
        execution = _execution(row)
        attempt_row = db.execute(
            "SELECT * FROM attempts WHERE execution_id=? AND attempt_ordinal=?",
            (execution_id, execution.current_attempt_ordinal),
        ).fetchone()
        attempt = _attempt(attempt_row) if attempt_row else None
        links = tuple(
            ExecutionClaimLink(r["execution_id"], r["claim_id"], r["ordinal"])
            for r in db.execute(
                "SELECT * FROM execution_claims WHERE execution_id=? ORDER BY ordinal",
                (execution_id,),
            )
        )
        claims = tuple(self.claim(link.claim_id) for link in links)
        replay_row = self._row("replay_assessments", "execution_id", execution_id)
        replay = (
            ReplayAssessment(
                execution_id,
                ReplayFact(replay_row["facts"]),
                bool(replay_row["replay_safe"]),
                bool(replay_row["side_effects_possible"]),
                replay_row["revision"],
            )
            if replay_row
            else None
        )
        obligation_row = self._row("obligations", "execution_id", execution_id)
        obligation = (
            ResponseObligation(
                execution_id,
                obligation_row["exact_target"],
                ObligationState(obligation_row["state"]),
                obligation_row["reason_code"],
                obligation_row["created_at_ms"],
                obligation_row["updated_at_ms"],
                obligation_row["revision"],
                obligation_row["receipt_message_id"],
                obligation_row["receipt_seq"],
            )
            if obligation_row
            else None
        )
        # Read-only projections do not grant Tx1, append, Tx2 or resolution.
        intent_row = self._row("publication_intents", "execution_id", execution_id)
        intent = (
            PublicationIntent(
                execution_id,
                intent_row["sender"],
                intent_row["exact_target"],
                MessageType(intent_row["message_type"]),
                bool(intent_row["notice"]),
                intent_row["timestamp"],
                intent_row["payload"],
                intent_row["payload_digest"],
                intent_row["publication_key"],
                intent_row["expected_message_id"],
            )
            if intent_row
            else None
        )
        receipt_row = self._row("publication_receipts", "execution_id", execution_id)
        receipt = (
            PublicationReceipt(
                execution_id,
                intent.publication_key,
                receipt_row["seq"],
                receipt_row["message_id"],
                intent.sender,
                intent.exact_target,
                intent.message_type,
                intent.notice,
                intent.timestamp,
                intent.payload_digest,
            )
            if receipt_row and intent is not None
            else None
        )
        if receipt_row is not None and intent is None:
            raise IntegrityViolationError("publication receipt has no frozen intent")
        connectivity_row = self._row("connectivity", "execution_id", execution_id)
        connectivity = (
            ConnectivityFacet(
                execution_id,
                OwnerConnectivity(connectivity_row["owner_state"]),
                ACPClientConnectivity(connectivity_row["acp_client_state"]),
                connectivity_row["revision"],
                connectivity_row["observed_at_ms"],
            )
            if connectivity_row
            else None
        )
        audit_row = db.execute(
            "SELECT * FROM recovery_audit WHERE execution_id=? ORDER BY audit_id DESC LIMIT 1",
            (execution_id,),
        ).fetchone()
        audit = (
            RecoveryAudit(
                execution_id,
                RecoveryKind(audit_row["kind"]),
                audit_row["reason_code"],
                audit_row["sanitized_detail"],
                audit_row["attempt"],
                audit_row["elapsed_ms"],
                audit_row["observed_at_ms"],
            )
            if audit_row
            else None
        )
        pointer = self._participant(execution.owner_lookup).pointer
        return RecoverySnapshot(
            execution,
            attempt,
            claims,
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

    def create_execution(
        self,
        execution_id: str,
        origin: ExecutionOrigin,
        owner_lookup: str,
        owner_thread: str,
        max_attempts: int,
        *,
        claim_ids: tuple[str, ...] = (),
        exact_target: str | None = None,
    ) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
        origin = ExecutionOrigin(origin)
        if origin is ExecutionOrigin.WIRE and (not claim_ids or not exact_target):
            raise IdentityConflict("wire execution requires ordered claims and target")
        if origin is not ExecutionOrigin.WIRE and (claim_ids or exact_target is not None):
            raise IdentityConflict("claimless execution cannot bind claims")
        with self._transaction() as db:
            row = self._row("executions", "execution_id", execution_id)
            if row is not None:
                snapshot = self.snapshot(execution_id)
                e = snapshot.execution
                if (
                    e.origin,
                    e.owner_lookup,
                    e.owner_thread,
                    e.max_attempts,
                    e.exact_target,
                    tuple(link.claim_id for link in snapshot.links),
                ) != (origin, owner_lookup, owner_thread, max_attempts, exact_target, claim_ids):
                    raise IdentityConflict("execution identity conflicts")
                return AlreadyApplied(snapshot)
            participant = self._participant(owner_lookup)
            if not participant.committed or participant.owner_thread != owner_thread:
                raise IdentityConflict("execution requires committed current owner")
            if not isinstance(max_attempts, int) or max_attempts <= 0:
                raise ValueError("max_attempts must be positive")
            if len(set(claim_ids)) != len(claim_ids):
                raise IdentityConflict("duplicate claim membership")
            now = self._now()
            db.execute(
                "INSERT INTO executions(execution_id,origin,status,exact_target,owner_thread,"
                "owner_lookup,revision,current_attempt_ordinal,max_attempts,reason_code,"
                "created_at_ms,updated_at_ms) VALUES (?,?,?,?,?,?,1,NULL,?,NULL,?,?)",
                (
                    execution_id,
                    origin.value,
                    ExecutionStatus.QUEUED.value,
                    exact_target,
                    owner_thread,
                    owner_lookup,
                    max_attempts,
                    now,
                    now,
                ),
            )
            for ordinal, claim_id in enumerate(claim_ids):
                claim = self.claim(claim_id)
                if (
                    claim.recipient_lookup != owner_lookup
                    or claim.execution_id is not None
                    or claim.disposition
                    not in {
                        ClaimDisposition.TRIAGE_PENDING,
                        ClaimDisposition.FULL_PENDING,
                        ClaimDisposition.DEFERRED,
                    }
                ):
                    raise IdentityConflict("claim cannot engage this execution")
                verdict = (
                    TriageVerdict.ENGAGE if claim.wake_mode is WakeMode.BOUNDED_TRIAGE else None
                )
                db.execute(
                    "UPDATE wake_claims SET disposition='engaged',triage_verdict=?,exact_target=?,"
                    "execution_id=?,revision=revision+1,updated_at_ms=? WHERE claim_id=?",
                    (
                        verdict.value if verdict else None,
                        exact_target,
                        execution_id,
                        self._now(claim.updated_at_ms),
                        claim_id,
                    ),
                )
                db.execute(
                    "INSERT INTO execution_claims VALUES (?,?,?)", (execution_id, claim_id, ordinal)
                )
            if origin is ExecutionOrigin.WIRE:
                db.execute(
                    "INSERT INTO obligations(execution_id,exact_target,state,reason_code,"
                    "created_at_ms,updated_at_ms,revision,receipt_message_id,receipt_seq) "
                    "VALUES (?,?,'pending',NULL,?,?,1,NULL,NULL)",
                    (execution_id, exact_target, now, now),
                )
            return Applied(self.snapshot(execution_id))

    def mark_pending(
        self, execution_id: str, *, expected_revision: int
    ) -> Applied[RecoverySnapshot]:
        with self._transaction() as db:
            snapshot = self.snapshot(execution_id)
            if snapshot.execution.revision != expected_revision:
                raise StaleRevision("execution revision changed")
            if snapshot.execution.status is not ExecutionStatus.QUEUED:
                raise IdentityConflict("only queued executions can become pending")
            db.execute(
                "UPDATE executions SET status='pending',revision=revision+1,"
                "updated_at_ms=? WHERE execution_id=? AND revision=?",
                (self._now(snapshot.execution.updated_at_ms), execution_id, expected_revision),
            )
            return Applied(self.snapshot(execution_id))

    def fail_unstarted(
        self,
        execution_id: str,
        *,
        expected_revision: int,
        reason_code: str,
    ) -> Applied[RecoverySnapshot]:
        """Fail queued/pending work; v2 cannot resume a pre-attempt deferral."""
        _bounded_reason(reason_code)
        if not reason_code:
            raise ValueError("unstarted settlement requires a reason")
        with self._transaction() as db:
            before = self.snapshot(execution_id)
            execution = before.execution
            if execution.revision != expected_revision:
                raise StaleRevision("execution revision changed")
            if execution.current_attempt_ordinal is not None or execution.status not in {
                ExecutionStatus.QUEUED,
                ExecutionStatus.PENDING,
            }:
                raise IdentityConflict("unstarted failure requires queued/pending work")
            state = "failed"
            now = self._now(execution.updated_at_ms)
            db.execute(
                "UPDATE executions SET status=?,revision=revision+1,"
                "reason_code=?,updated_at_ms=? WHERE execution_id=?",
                (state, reason_code, now, execution_id),
            )
            if before.obligation is not None:
                db.execute(
                    "UPDATE obligations SET state=?,revision=revision+1,"
                    "reason_code=?,updated_at_ms=? WHERE execution_id=?",
                    (state, reason_code, self._now(before.obligation.updated_at_ms), execution_id),
                )
            for claim in before.claims:
                db.execute(
                    "UPDATE wake_claims SET disposition=?,revision=revision+1,"
                    "updated_at_ms=? WHERE claim_id=?",
                    (state, self._now(claim.updated_at_ms), claim.claim_id),
                )
            return Applied(self.snapshot(execution_id))

    def _assert_fence(self, fence: OwnerFence) -> tuple[RecoverySnapshot, AttemptRecord]:
        snapshot = self.snapshot(fence.execution_id)
        attempt = snapshot.attempt
        if (
            not snapshot.is_current
            or attempt is None
            or attempt.attempt_ordinal != fence.attempt_ordinal
            or attempt.owner_thread != fence.owner_thread
            or attempt.owner_generation != fence.owner_generation
            or attempt.owner_token_digest != _digest(fence.token)
            or self._participant(attempt.owner_lookup).generation != fence.owner_generation
            or self._participant(attempt.owner_lookup).owner_thread != fence.owner_thread
        ):
            raise StaleFence("attempt fence is not current")
        if attempt.revision != fence.revision:
            raise StaleRevision("attempt fence revision changed")
        return snapshot, attempt

    def start_attempt(
        self,
        execution_id: str,
        attempt_ordinal: int,
        owner_thread: str,
        owner_generation: int,
        token: str,
        *,
        expected_execution_revision: int,
        expected_pointer_revision: int,
    ) -> Applied[StartResult] | AlreadyApplied[StartResult]:
        digest = _digest(token)
        with self._transaction() as db:
            snapshot = self.snapshot(execution_id)
            execution = snapshot.execution
            ordinal = (
                1
                if execution.current_attempt_ordinal is None
                else execution.current_attempt_ordinal + 1
            )
            # Replay identity is immutable creation identity, NEVER mutable phase,
            # lease, replay, claims, obligation, pointer or CAS revisions.
            prior = db.execute(
                "SELECT * FROM attempts WHERE execution_id=? AND owner_token_digest=?",
                (execution_id, digest),
            ).fetchone()
            if prior is not None:
                old = _attempt(prior)
                if (
                    old.attempt_ordinal,
                    old.owner_lookup,
                    old.owner_thread,
                    old.owner_generation,
                    old.created_at_ms >= execution.created_at_ms,
                ) != (
                    attempt_ordinal,
                    execution.owner_lookup,
                    owner_thread,
                    owner_generation,
                    True,
                ):
                    raise IdentityConflict("prepared token is bound to a different attempt")
                return AlreadyApplied(
                    StartResult(
                        snapshot,
                        OwnerFence(
                            execution_id,
                            old.attempt_ordinal,
                            owner_thread,
                            owner_generation,
                            old.revision,
                            token,
                        ),
                    )
                )
            if db.execute(
                "SELECT 1 FROM attempts WHERE owner_token_digest=?", (digest,)
            ).fetchone():
                raise IdentityConflict("prepared fence token has already been issued")
            participant = self._participant(execution.owner_lookup)
            if not participant.committed or (participant.owner_thread, participant.generation) != (
                owner_thread,
                owner_generation,
            ):
                raise StaleFence("owner generation or thread changed")
            if (execution.revision, participant.pointer.pointer_revision) != (
                expected_execution_revision,
                expected_pointer_revision,
            ):
                raise StaleRevision("execution or pointer revision changed")
            if participant.pointer.execution_id is not None:
                raise StaleFence("owner has a current attempt")
            if execution.status not in {ExecutionStatus.PENDING, ExecutionStatus.DEFERRED}:
                raise IdentityConflict("execution cannot start an attempt")
            if snapshot.publication_intent is not None:
                raise PublicationUncertain("no authorized bus-keyed publication resolution")
            if execution.status is ExecutionStatus.DEFERRED and snapshot.attempt is None:
                raise RecoveryBlocked("frozen v2 cannot resume an unstarted deferral")
            if execution.status is ExecutionStatus.DEFERRED and not snapshot.can_retry:
                raise RecoveryBlocked("retry requires final death, replay proof, and budget")
            if ordinal != attempt_ordinal:
                raise IdentityConflict("attempt ordinal must be contiguous")
            if ordinal > execution.max_attempts:
                raise RecoveryBlocked("retry budget exhausted")
            if (
                snapshot.attempt is not None
                and owner_generation <= snapshot.attempt.owner_generation
            ):
                raise StaleFence("retry must use a strictly newer owner generation")
            created = self._now(execution.updated_at_ms)
            # Versioned fixed policy: no caller-selected initial lease, no semantic mirror.
            lease = created + INITIAL_LEASE_DURATION_MS
            db.execute(
                "INSERT INTO attempts(execution_id,attempt_ordinal,owner_lookup,owner_thread,"
                "owner_generation,owner_token_digest,phase,revision,lease_expires_at_ms,"
                "last_progress_at_ms,backend_done,process_dead,reason_code,"
                "created_at_ms,updated_at_ms) "
                "VALUES (?,?,?,?,?,?,'prompt_starting',1,?,NULL,0,0,NULL,?,?)",
                (
                    execution_id,
                    ordinal,
                    execution.owner_lookup,
                    owner_thread,
                    owner_generation,
                    digest,
                    lease,
                    created,
                    created,
                ),
            )
            db.execute(
                "UPDATE executions SET status='active',current_attempt_ordinal=?,"
                "revision=revision+1,reason_code=NULL,updated_at_ms=? WHERE execution_id=?",
                (ordinal, created, execution_id),
            )
            db.execute(
                "UPDATE current_executions SET execution_id=?,attempt_ordinal=?,"
                "pointer_revision=pointer_revision+1 WHERE owner_lookup=?",
                (execution_id, ordinal, execution.owner_lookup),
            )
            if attempt_ordinal > 1 and snapshot.replay is not None and snapshot.replay.replay_safe:
                # Replay safety is execution-scoped in v2, not bound to the new
                # attempt.  Monotonically revoke the previous attempt's proof;
                # a further automatic retry needs a separately versioned schema.
                updated = db.execute(
                    "UPDATE replay_assessments SET replay_safe=0,revision=revision+1 "
                    "WHERE execution_id=?",
                    (execution_id,),
                )
                if updated.rowcount != 1:
                    raise IntegrityViolationError("retry safety revocation lost its assessment")
            if (
                execution.origin is ExecutionOrigin.WIRE
                and snapshot.obligation is not None
                and snapshot.obligation.state is ObligationState.DEFERRED
            ):
                db.execute(
                    "UPDATE obligations SET state='pending',revision=revision+1,"
                    "reason_code=NULL,updated_at_ms=? WHERE execution_id=?",
                    (self._now(snapshot.obligation.updated_at_ms), execution_id),
                )
            if execution.status is ExecutionStatus.DEFERRED:
                for claim in snapshot.claims:
                    db.execute(
                        "UPDATE wake_claims SET disposition='engaged',revision=revision+1,"
                        "updated_at_ms=? WHERE claim_id=?",
                        (self._now(claim.updated_at_ms), claim.claim_id),
                    )
            current = self.snapshot(execution_id)
            return Applied(
                StartResult(
                    current,
                    OwnerFence(execution_id, ordinal, owner_thread, owner_generation, 1, token),
                )
            )

    def renew_attempt_lease(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        duration_ms: int,
    ) -> Applied[StartResult]:
        if not 1 <= duration_ms <= MAX_LEASE_RENEWAL_MS:
            raise ValueError("renewal duration is outside the bounded policy")
        with self._transaction() as db:
            snapshot, attempt = self._assert_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            if attempt.process_dead:
                raise RecoveryBlocked("a dead Pi RPC subprocess cannot renew its live lease")
            expiry = max(
                attempt.lease_expires_at_ms or 0, self._now(attempt.updated_at_ms) + duration_ms
            )
            db.execute(
                "UPDATE attempts SET lease_expires_at_ms=?,revision=revision+1,"
                "updated_at_ms=? WHERE execution_id=? AND attempt_ordinal=?",
                (
                    expiry,
                    self._now(attempt.updated_at_ms),
                    fence.execution_id,
                    fence.attempt_ordinal,
                ),
            )
            after = self.snapshot(fence.execution_id)
            assert after.attempt is not None
            return Applied(StartResult(after, replace(fence, revision=after.attempt.revision)))

    def advance_attempt(
        self,
        fence: OwnerFence,
        phase: AttemptPhase,
        *,
        expected_pointer_revision: int,
        backend_done: bool = False,
        process_dead: bool = False,
        progress: bool = False,
        reason_code: str | None = None,
    ) -> Applied[StartResult]:
        phase = AttemptPhase(phase)
        _bounded_reason(reason_code)
        if any(type(value) is not bool for value in (backend_done, process_dead, progress)):
            raise ValueError("attempt finality and progress must be booleans")
        if phase not in ACTIVE_ATTEMPT_PHASES:
            raise IdentityConflict("terminal phases require atomic settlement")
        with self._transaction() as db:
            snapshot, attempt = self._assert_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            if phase != attempt.phase and phase not in ATTEMPT_PHASE_TRANSITIONS[attempt.phase]:
                raise IdentityConflict("attempt phase edge is not declared")
            if attempt.process_dead or attempt.backend_done:
                # Once either finality fact is recorded, the backend cannot
                # emit another phase or progress observation.  The other fact
                # may arrive later on the SAME phase before atomic settlement.
                new_final_fact = (backend_done and not attempt.backend_done) or (
                    process_dead and not attempt.process_dead
                )
                if phase != attempt.phase or progress or not new_final_fact:
                    raise RecoveryBlocked(
                        "final backend evidence forbids further phase or progress"
                    )
            now = self._now(attempt.updated_at_ms)
            db.execute(
                "UPDATE attempts SET phase=?,revision=revision+1,updated_at_ms=?,"
                "last_progress_at_ms=?,backend_done=?,process_dead=?,reason_code=? "
                "WHERE execution_id=? AND attempt_ordinal=?",
                (
                    phase.value,
                    now,
                    now if progress else attempt.last_progress_at_ms,
                    int(attempt.backend_done or backend_done),
                    int(attempt.process_dead or process_dead),
                    reason_code,
                    fence.execution_id,
                    fence.attempt_ordinal,
                ),
            )
            after = self.snapshot(fence.execution_id)
            assert after.attempt is not None
            return Applied(StartResult(after, replace(fence, revision=after.attempt.revision)))

    def observe_replay(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        expected_replay_revision: int | None,
        facts: ReplayFact,
        replay_safe: bool,
        side_effects_possible: bool,
    ) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
        if type(replay_safe) is not bool or type(side_effects_possible) is not bool:
            raise ValueError("replay assessment flags must be booleans")
        facts = ReplayFact(facts)
        with self._transaction() as db:
            snapshot, _ = self._assert_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            before = snapshot.replay
            if (before.revision if before else None) != expected_replay_revision:
                raise StaleRevision("replay assessment revision changed")
            after = ReplayAssessment(
                fence.execution_id,
                facts,
                replay_safe,
                side_effects_possible,
                1 if before is None else before.revision + 1,
            )
            if before is not None:
                if (before.facts, before.replay_safe, before.side_effects_possible) == (
                    after.facts,
                    after.replay_safe,
                    after.side_effects_possible,
                ):
                    return AlreadyApplied(snapshot)
                if (
                    (before.facts | after.facts) != after.facts
                    or (not before.replay_safe and after.replay_safe)
                    or (before.side_effects_possible and not after.side_effects_possible)
                ):
                    raise IdentityConflict("replay facts cannot be erased")
                db.execute(
                    "UPDATE replay_assessments SET facts=?,replay_safe=?,"
                    "side_effects_possible=?,revision=revision+1 WHERE execution_id=?",
                    (int(facts), int(replay_safe), int(side_effects_possible), fence.execution_id),
                )
            else:
                db.execute(
                    "INSERT INTO replay_assessments VALUES (?,?,?,?,?)",
                    (
                        fence.execution_id,
                        int(facts),
                        int(replay_safe),
                        int(side_effects_possible),
                        1,
                    ),
                )
            return Applied(self.snapshot(fence.execution_id))

    def observe_connectivity(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        expected_revision: int | None,
        owner: OwnerConnectivity,
        acp_client: ACPClientConnectivity,
    ) -> Applied[RecoverySnapshot]:
        owner, acp_client = OwnerConnectivity(owner), ACPClientConnectivity(acp_client)
        with self._transaction() as db:
            snapshot, _ = self._assert_fence(fence)
            before = snapshot.connectivity
            if (
                snapshot.pointer_revision != expected_pointer_revision
                or (before.revision if before else None) != expected_revision
            ):
                raise StaleRevision("connectivity or pointer revision changed")
            now = self._now(before.observed_at_ms if before else 0)
            if before:
                db.execute(
                    "UPDATE connectivity SET owner_state=?,acp_client_state=?,"
                    "revision=revision+1,observed_at_ms=? WHERE execution_id=?",
                    (owner.value, acp_client.value, now, fence.execution_id),
                )
            else:
                db.execute(
                    "INSERT INTO connectivity VALUES (?,?,?,?,?)",
                    (fence.execution_id, owner.value, acp_client.value, 1, now),
                )
            return Applied(self.snapshot(fence.execution_id))

    def append_recovery_audit(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        kind: RecoveryKind,
        reason_code: str,
        sanitized_detail: str | None = None,
        elapsed_ms: int = 0,
    ) -> Applied[RecoverySnapshot]:
        kind = RecoveryKind(kind)
        _bounded_reason(reason_code)
        if (
            reason_code is None
            or elapsed_ms < 0
            or (sanitized_detail is not None and len(sanitized_detail) > MAX_SANITIZED_DETAIL_CHARS)
        ):
            raise ValueError("invalid recovery audit")
        with self._transaction() as db:
            snapshot, attempt = self._assert_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            if kind in {RecoveryKind.DEFERRED, RecoveryKind.FAILED}:
                raise IdentityConflict("terminal audit belongs to atomic settlement")
            if kind is RecoveryKind.RECOVERED:
                incident = snapshot.last_recovery
                if (
                    snapshot.execution.status is not ExecutionStatus.ACTIVE
                    or attempt.phase is not AttemptPhase.MODEL_RUNNING
                    or incident is None
                    or incident.attempt != attempt.attempt_ordinal
                    or incident.kind
                    not in {
                        RecoveryKind.MODEL_STALLED,
                        RecoveryKind.ABORTING,
                        RecoveryKind.RETRYING,
                        RecoveryKind.PROVIDER_UNAVAILABLE,
                    }
                ):
                    raise IdentityConflict(
                        "recovered requires a current unresolved incident and resumed model"
                    )
            elif kind.value != attempt.phase.value:
                raise IdentityConflict("recovery audit must match observed attempt phase")
            db.execute(
                "INSERT INTO recovery_audit(execution_id,kind,reason_code,"
                "sanitized_detail,attempt,elapsed_ms,observed_at_ms) VALUES (?,?,?,?,?,?,?)",
                (
                    fence.execution_id,
                    kind.value,
                    reason_code,
                    sanitized_detail,
                    attempt.attempt_ordinal,
                    elapsed_ms,
                    self._now(attempt.updated_at_ms),
                ),
            )
            return Applied(self.snapshot(fence.execution_id))

    def _settle(
        self,
        snapshot: RecoverySnapshot,
        *,
        success: bool,
        reason_code: str | None,
    ) -> RecoverySnapshot:
        db = self._connection
        execution, attempt = snapshot.execution, snapshot.attempt
        if snapshot.publication_intent is not None:
            raise PublicationUncertain("publication requires bus-keyed receipt resolution")
        if (
            attempt is None
            or not snapshot.is_current
            or not (attempt.backend_done and attempt.process_dead)
        ):
            raise RecoveryBlocked("settlement requires exact final done/death evidence")
        if success:
            if attempt.phase is not AttemptPhase.SETTLING:
                raise IdentityConflict("silent completion requires settling phase")
            if snapshot.obligation is not None and snapshot.obligation.state not in {
                ObligationState.PENDING,
                ObligationState.DEFERRED,
            }:
                raise IdentityConflict("wire completion requires nonpublication obligation")
            status, phase, disposition = "completed", "succeeded", "completed"
        else:
            authorized = retry_disposition_authorized(
                execution, snapshot.replay, snapshot.obligation
            )
            status = "deferred" if authorized else "failed"
            phase = "attempt_failed"
            disposition = status
        now = self._now(max(execution.updated_at_ms, attempt.updated_at_ms))
        db.execute(
            "UPDATE attempts SET phase=?,lease_expires_at_ms=NULL,"
            "revision=revision+1,updated_at_ms=?,reason_code=? "
            "WHERE execution_id=? AND attempt_ordinal=?",
            (phase, now, reason_code, execution.execution_id, attempt.attempt_ordinal),
        )
        db.execute(
            "UPDATE executions SET status=?,revision=revision+1,"
            "updated_at_ms=?,reason_code=? WHERE execution_id=?",
            (status, now, reason_code, execution.execution_id),
        )
        if snapshot.obligation is not None:
            obligation = snapshot.obligation
            target = "silent" if success else ("deferred" if authorized else "failed")
            db.execute(
                "UPDATE obligations SET state=?,revision=revision+1,"
                "updated_at_ms=?,reason_code=? WHERE execution_id=?",
                (target, self._now(obligation.updated_at_ms), reason_code, execution.execution_id),
            )
        for claim in snapshot.claims:
            db.execute(
                "UPDATE wake_claims SET disposition=?,revision=revision+1,"
                "updated_at_ms=? WHERE claim_id=?",
                (disposition, self._now(claim.updated_at_ms), claim.claim_id),
            )
        db.execute(
            "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
            "pointer_revision=pointer_revision+1 WHERE owner_lookup=?",
            (execution.owner_lookup,),
        )
        return self.snapshot(execution.execution_id)

    def settle_nonpublication(
        self,
        fence: OwnerFence,
        *,
        expected_pointer_revision: int,
        success: bool,
        reason_code: str | None = None,
    ) -> Applied[RecoverySnapshot]:
        if type(success) is not bool:
            raise ValueError("settlement success must be a boolean")
        _bounded_reason(reason_code)
        with self._transaction():
            snapshot, _ = self._assert_fence(fence)
            if snapshot.pointer_revision != expected_pointer_revision:
                raise StaleRevision("pointer revision changed")
            return Applied(self._settle(snapshot, success=success, reason_code=reason_code))


_MONITOR_GRANT = object()


class RecoveryMonitorCapability:
    """Separate attested dead-attempt authority; never a live owner fence.

    Construction is deliberately unavailable from MutationStore's public API.
    A future trusted runtime must explicitly bind this private grant to an
    independently authenticated monitor; ordinary callers cannot obtain it via
    registration, a token, or a recovered snapshot.
    """

    def __init__(self, store: MutationStore, *, _grant: object) -> None:
        if _grant is not _MONITOR_GRANT:
            raise PermissionError("recovery monitor requires trusted construction")
        self._store = store

    def terminalize_dead_attempt(
        self,
        execution_id: str,
        ordinal: int,
        owner_generation: int,
        *,
        expected_attempt_revision: int,
        expected_execution_revision: int,
        expected_pointer_revision: int,
        evidence: MonitorEvidence,
        owner_loss: VerifiedOwnerLoss | None = None,
        replay_facts: ReplayFact = ReplayFact.NONE,
    ) -> Applied[RecoverySnapshot]:
        if not evidence.subprocess_dead:
            raise RecoveryBlocked("monitor cannot record a live Pi RPC subprocess as dead")
        store = self._store
        replay_facts = ReplayFact(replay_facts)
        with store._transaction() as db:
            snapshot = store.snapshot(execution_id)
            attempt = snapshot.attempt
            if (
                not snapshot.is_current
                or attempt is None
                or attempt.attempt_ordinal != ordinal
                or attempt.owner_generation != owner_generation
            ):
                raise StaleFence("monitor must name the exact old active attempt")
            if (attempt.revision, snapshot.execution.revision, snapshot.pointer_revision) != (
                expected_attempt_revision,
                expected_execution_revision,
                expected_pointer_revision,
            ):
                raise StaleRevision("monitor CAS is stale")
            if not isinstance(owner_loss, VerifiedOwnerLoss) or not _owner_loss_verified(
                owner_loss, execution_id, attempt.owner_lookup, owner_generation, ordinal
            ):
                raise RecoveryBlocked("authenticated old-owner-loss verifier is not activated")
            now = store._now(max(attempt.updated_at_ms, evidence.observed_at_ms))
            # Preserve previously known safety only when trusted evidence says
            # effects were observed rather than guessed unknown.  Never assert
            # new safety, even with an empty observation.
            new_facts = replay_facts | (
                ReplayFact.UNKNOWN_EFFECTS if evidence.unknown_effects else ReplayFact.NONE
            )
            before = snapshot.replay
            if before is not None:
                new_facts |= before.facts
                unsafe = bool(new_facts) or not before.replay_safe
                possible = (
                    before.side_effects_possible or evidence.unknown_effects or bool(replay_facts)
                )
                if (before.facts, before.replay_safe, before.side_effects_possible) != (
                    new_facts,
                    not unsafe,
                    possible,
                ):
                    db.execute(
                        "UPDATE replay_assessments SET facts=?,replay_safe=?,"
                        "side_effects_possible=?,revision=revision+1 WHERE execution_id=?",
                        (int(new_facts), int(not unsafe), int(possible), execution_id),
                    )
            elif new_facts:
                db.execute(
                    "INSERT INTO replay_assessments VALUES (?,?,0,1,1)",
                    (execution_id, int(new_facts)),
                )
            db.execute(
                "UPDATE attempts SET process_dead=1,backend_done=?,revision=revision+1,"
                "updated_at_ms=?,reason_code=? WHERE execution_id=? AND attempt_ordinal=?",
                (
                    int(attempt.backend_done or evidence.backend_done),
                    now,
                    evidence.reason_code,
                    execution_id,
                    ordinal,
                ),
            )
            unresolved = snapshot.publication_intent is not None
            if (attempt.backend_done or evidence.backend_done) and not unresolved:
                settled = store._settle(
                    store.snapshot(execution_id),
                    success=False,
                    reason_code=evidence.reason_code,
                )
                kind = (
                    RecoveryKind.DEFERRED
                    if settled.execution.status is ExecutionStatus.DEFERRED
                    else RecoveryKind.FAILED
                )
                db.execute(
                    "INSERT INTO recovery_audit(execution_id,kind,reason_code,"
                    "sanitized_detail,attempt,elapsed_ms,observed_at_ms) "
                    "VALUES (?,?,?,NULL,?,0,?)",
                    (execution_id, kind.value, evidence.reason_code, ordinal, now),
                )
                return Applied(store.snapshot(execution_id))
            # Death and genuinely observed backend completion commit even when
            # publication remains unresolved.  Neither state clears the pointer.
        if unresolved and (attempt.backend_done or evidence.backend_done):
            raise PublicationUncertain("frozen publication requires bus-keyed receipt")
        raise RecoveryBlocked("process death alone is not backend-final proof")
