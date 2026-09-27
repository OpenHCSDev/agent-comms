"""Default-OFF, provider-free retained-child settlement prototype.

There is deliberately no ACP or Pi caller. An externally supplied event is not
in-child evidence: test admission is explicitly disabled on ordinary stores.
PR116 owns any production migration of this fresh-root schema.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .coordination import AttemptPhase, OwnerFence, WakeMode
from .coordination_store import (
    AlreadyApplied,
    Applied,
    IdentityConflict,
    MutationStore,
    RecoveryBlocked,
    StaleFence,
    StaleRevision,
    StartResult,
)
from .declarations import MessageBus
from .wake import WakeDecision


@dataclass(frozen=True, slots=True)
class RetainedTurnIdentity:
    input_id: str
    session_id: str
    session_file: str
    child_pid: int
    child_nonce: str
    turn_id: str
    turn_generation: int
    claim_id: str
    claim_revision: int

    def __post_init__(self) -> None:
        for field in (
            "input_id",
            "session_id",
            "session_file",
            "child_nonce",
            "turn_id",
            "claim_id",
        ):
            value = getattr(self, field)
            if type(value) is not str or not value or len(value) > 1024:
                raise ValueError(f"invalid retained turn {field}")
        if type(self.child_pid) is not int or self.child_pid <= 0:
            raise ValueError("invalid retained child pid")
        if type(self.turn_generation) is not int or self.turn_generation <= 0:
            raise ValueError("invalid retained turn generation")
        if type(self.claim_revision) is not int or self.claim_revision <= 0:
            raise ValueError("invalid retained claim revision")


@dataclass(frozen=True, slots=True)
class RetainedTurnReceipt:
    identity: RetainedTurnIdentity
    terminal_sequence: int
    terminal_digest: str

    def __post_init__(self) -> None:
        if type(self.terminal_sequence) is not int or self.terminal_sequence <= 0:
            raise ValueError("invalid terminal sequence")
        if (
            type(self.terminal_digest) is not str
            or len(self.terminal_digest) != 64
            or any(c not in "0123456789abcdef" for c in self.terminal_digest)
        ):
            raise ValueError("invalid terminal digest")


def _enabled(store: MutationStore) -> None:
    if not store._test_retained_turns:
        raise RecoveryBlocked("retained-child settlement is OFF without an in-child authority")


def _admission(db: sqlite3.Connection, fence: OwnerFence) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM retained_turn_receipts WHERE execution_id=? AND attempt_ordinal=?",
        (fence.execution_id, fence.attempt_ordinal),
    ).fetchone()


def _matches(row: sqlite3.Row, identity: RetainedTurnIdentity) -> bool:
    return all(row[name] == getattr(identity, name) for name in identity.__dataclass_fields__)


def admit_test_retained_turn(
    store: MutationStore,
    bus: MessageBus,
    wire_root_id: str,
    fence: OwnerFence,
    identity: RetainedTurnIdentity,
) -> Applied[StartResult]:
    """Test-only: bind a verified original FULL source and fresh claim once.

    The committed bus row is immutable; this OFF-only read is NOT an atomic
    source/registry/SQL grant for raw bytes or ACP admission.
    """
    _enabled(store)
    if type(identity) is not RetainedTurnIdentity or type(bus) is not MessageBus:
        raise TypeError("exact retained turn identity and bus required")
    if type(wire_root_id) is not str or not wire_root_id:
        raise ValueError("retained turn requires original wire root")
    candidate = store.snapshot(fence.execution_id).claims
    if len(candidate) != 1 or candidate[0].claim_id != identity.claim_id:
        raise StaleFence("retained turn requires sole original execution claim")
    # Read the committed original BEFORE the SQL lock; it is immutable. Recheck
    # the FULL claim's exact sequence/ID/revision under the SQL transaction.
    source = bus.read_initial_cohort(wire_root_id, candidate[0].wire_seq)
    with store._transaction() as db:
        snapshot, attempt = store._assert_fence(fence)
        if (
            attempt.phase is not AttemptPhase.PROMPT_STARTING
            or attempt.completion_kind != "one_shot"
        ):
            raise RecoveryBlocked("retained identity must precede prompt admission")
        if len(snapshot.claims) != 1 or snapshot.claims[0].claim_id != identity.claim_id:
            raise StaleFence("retained identity requires the sole execution claim")
        claim = snapshot.claims[0]
        if claim.revision != identity.claim_revision or claim.disposition.value != "engaged":
            raise StaleRevision("retained claim changed before admission")
        if claim.wake_mode is not WakeMode.FULL:
            raise StaleFence("retained turn requires original selected FULL claim")
        matching = [
            (recipient, decision)
            for recipient, decision in zip(
                source.audience.recipients, source.decisions, strict=True
            )
            if recipient.recipient_lookup == claim.recipient_lookup
        ]
        if (
            len(matching) != 1
            or source.message.message_id != claim.message_id
            or source.message.seq != claim.wire_seq
            or matching[0][0].canonical_thread != claim.recipient
            or type(matching[0][1]) is not WakeDecision
            or matching[0][1].wake_mode is not WakeMode.FULL
            or matching[0][1].audience is not claim.audience
            or matching[0][1].recipient != claim.recipient_lookup
        ):
            raise StaleFence("retained FULL claim does not match committed original bus row")
        if db.execute(
            "SELECT 1 FROM retained_turn_receipts WHERE input_id=? OR "
            "(session_id=? AND turn_id=? AND turn_generation=?)",
            (identity.input_id, identity.session_id, identity.turn_id, identity.turn_generation),
        ).fetchone():
            raise IdentityConflict(
                "retained input or session turn already belongs to another attempt"
            )
        db.execute(
            "INSERT INTO retained_turn_receipts "
            "(execution_id,attempt_ordinal,owner_lookup,owner_thread,owner_generation,"
            "input_id,session_id,session_file,child_pid,child_nonce,turn_id,turn_generation,"
            "claim_id,claim_revision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fence.execution_id,
                fence.attempt_ordinal,
                attempt.owner_lookup,
                fence.owner_thread,
                fence.owner_generation,
                *(getattr(identity, name) for name in identity.__dataclass_fields__),
            ),
        )
        db.execute(
            "UPDATE attempts SET completion_kind='retained_turn',revision=revision+1,"
            "updated_at_ms=? WHERE execution_id=? AND attempt_ordinal=?",
            (store._now(attempt.updated_at_ms), fence.execution_id, fence.attempt_ordinal),
        )
        after = store.snapshot(fence.execution_id)
        assert after.attempt is not None
        from dataclasses import replace

        return Applied(StartResult(after, replace(fence, revision=after.attempt.revision)))


def record_test_turn_settled(
    store: MutationStore, fence: OwnerFence, receipt: RetainedTurnReceipt
) -> Applied[StartResult] | AlreadyApplied[StartResult]:
    """Test-only: compare complete identity then atomically persist a terminal fact.

    The production source must attest and durably corroborate this receipt first.
    No fake event accepted by this prototype is production authorization.
    """
    _enabled(store)
    if type(receipt) is not RetainedTurnReceipt:
        raise TypeError("exact retained terminal receipt required")
    with store._transaction() as db:
        snapshot, attempt = store._assert_fence(fence)
        row = _admission(db, fence)
        if (
            row is None
            or attempt.completion_kind != "retained_turn"
            or not _matches(row, receipt.identity)
        ):
            raise StaleFence("retained terminal identity differs from admission")
        if (
            attempt.phase is not AttemptPhase.SETTLING
            or not attempt.backend_done
            or attempt.process_dead
        ):
            raise RecoveryBlocked("retained receipt requires truthful final backend evidence")
        if len(snapshot.claims) != 1 or (
            snapshot.claims[0].claim_id != receipt.identity.claim_id
            or snapshot.claims[0].revision != receipt.identity.claim_revision
            or snapshot.claims[0].disposition.value != "engaged"
        ):
            raise StaleRevision("retained claim changed before settlement")
        if row["terminal_digest"] is not None:
            if (
                row["terminal_digest"] == receipt.terminal_digest
                and row["terminal_sequence"] == receipt.terminal_sequence
                and attempt.turn_settled
            ):
                return AlreadyApplied(StartResult(snapshot, fence))
            raise RecoveryBlocked("conflicting retained receipt is UNKNOWN")
        db.execute(
            "UPDATE retained_turn_receipts SET terminal_sequence=?,terminal_digest=? "
            "WHERE execution_id=? AND attempt_ordinal=? AND terminal_digest IS NULL",
            (
                receipt.terminal_sequence,
                receipt.terminal_digest,
                fence.execution_id,
                fence.attempt_ordinal,
            ),
        )
        db.execute(
            "UPDATE attempts SET turn_settled=1,revision=revision+1,updated_at_ms=? "
            "WHERE execution_id=? AND attempt_ordinal=? AND turn_settled=0",
            (store._now(attempt.updated_at_ms), fence.execution_id, fence.attempt_ordinal),
        )
        after = store.snapshot(fence.execution_id)
        assert after.attempt is not None
        from dataclasses import replace

        return Applied(StartResult(after, replace(fence, revision=after.attempt.revision)))


def require_retained_receipt(store: MutationStore, fence: OwnerFence) -> bool:
    """Return False for legacy, otherwise require the exact committed row."""
    snapshot, attempt = store._assert_fence(fence)
    if attempt.completion_kind == "one_shot":
        return False
    _enabled(store)
    row = _admission(store._connection, fence)
    if (
        row is None
        or not attempt.turn_settled
        or not attempt.backend_done
        # Actual child death after the committed terminal receipt is an
        # independent lifecycle fact; it does not erase the settled input.
        or row["terminal_digest"] is None
        or row["owner_lookup"] != attempt.owner_lookup
        or row["owner_thread"] != fence.owner_thread
        or row["owner_generation"] != fence.owner_generation
        or len(snapshot.claims) != 1
        or row["claim_id"] != snapshot.claims[0].claim_id
        or row["claim_revision"] != snapshot.claims[0].revision
    ):
        raise RecoveryBlocked("retained terminal receipt is absent or stale")
    return True
