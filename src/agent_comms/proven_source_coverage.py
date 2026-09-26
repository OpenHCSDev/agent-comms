"""Bounded, read-only historical coverage of private, frozen delivery sources.

This is NOT an inbox ACK or a work-selection cursor. In particular, an input
with UNKNOWN native outcome is a barrier, even when a later source has proof.
No missing claim is accepted, no native input is recovered/retried, and a
historical watermark grants no current-owner, provider, write or reply permit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .bus_publication import CommittedInitial
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination import WakeMode
from .coordination_cohort import _assert_schema, _receipt_matches
from .coordination_store import IdentityConflict, MutationStore
from .declarations import MessageBus, _store_lock
from .historical_native_inputs import read_historical_native_inputs
from .wake import NoWakeDecision, WakeDecision

_MAX_BUS_BYTES = 8 * 1024 * 1024
_MAX_BUS_ROWS = 1_000
_MAX_SCAN_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class ProvenSourceCoverage:
    wire_root_id: str
    recipient_lookup: str
    covered_seq: int
    injected_source_seqs: tuple[int, ...]
    no_wake_seqs: tuple[int, ...]
    blocked_seq: int | None


def read_proven_source_coverage(
    bus: MessageBus,
    store: MutationStore,
    *,
    wire_root_id: str,
    recipient_lookup: str,
    limit: int = 100,
) -> ProvenSourceCoverage:
    """Conservatively walk a bounded, *canonical* private initial snapshot.

    ``covered_seq`` means every initial in the snapshot through that sequence
    was either not addressed to this stable lookup, explicitly no-wake with a
    sealed N receipt, or has corroborated live-recorded native evidence for
    the necessary selected stage(s). It is not an injected-message cursor:
    no-wake and absent-audience rows are not injections. A selected unproven
    source stops the walk even if a later source is independently proven.
    This pilot refuses oversized bus bytes before the lock's durability scan,
    stops at the first over-budget row/initial, and applies a best-effort scan
    deadline. Filesystem fsync/locks are not a hard wall-clock deadline.
    """
    if (
        type(bus) is not MessageBus
        or type(store) is not MutationStore
        or type(wire_root_id) is not str
        or len(wire_root_id) != 32
        or any(ch not in "0123456789abcdef" for ch in wire_root_id)
        or type(recipient_lookup) is not str
        or len(recipient_lookup) != 32
        or any(ch not in "0123456789abcdef" for ch in recipient_lookup)
        or type(limit) is not int
        or not 0 < limit <= 100
    ):
        raise ValueError("source coverage needs exact private identities and bounded scan")
    if store._connection.in_transaction:
        raise IdentityConflict("source coverage requires a committed coordinator snapshot")
    deadline = time.monotonic() + _MAX_SCAN_SECONDS
    initials: list[CommittedInitial] = []
    with _store_lock(bus._path, blocking=False, max_bus_bytes=_MAX_BUS_BYTES):
        if time.monotonic() > deadline:
            raise IdentityConflict("source coverage exceeded its scan deadline")
        marker = bus._private_marker_unlocked()
        if marker["wire_root_id"] != wire_root_id:
            raise IdentityConflict("source coverage private wire root changed")
        for row_count, (_, _, initial) in enumerate(
            bus._verified_private_rows_unlocked(marker), start=1
        ):
            if row_count > _MAX_BUS_ROWS or time.monotonic() > deadline:
                raise IdentityConflict("source coverage exceeded row or scan deadline budget")
            if initial is not None:
                if len(initials) >= limit:
                    raise IdentityConflict(
                        "source coverage exceeded its bounded private initial scan"
                    )
                initials.append(initial)
    covered = 0
    injected: list[int] = []
    no_wake: list[int] = []
    blocked: int | None = None
    for initial in initials:
        seq = initial.message.seq
        matches = [
            (recipient, decision)
            for recipient, decision in zip(
                initial.audience.recipients, initial.decisions, strict=True
            )
            if recipient.recipient_lookup == recipient_lookup
        ]
        if len(matches) > 1:
            raise IdentityConflict("duplicate stable lookup in frozen audience")
        if not matches:
            covered = seq  # Canonical bus proves this source did not address us.
            continue
        recipient, decision = matches[0]
        with store._read_transaction():
            _assert_schema(store._connection)
            assert_native_runtime_schema(store._connection)
            sealed = store._connection.execute(
                "SELECT 1 FROM claim_batch_receipts WHERE wire_root_id=? "
                "AND wire_seq=? AND sealed=1",
                (wire_root_id, seq),
            ).fetchone()
            receipt = _receipt_matches(store._connection, initial) if sealed else None
        if receipt is None:
            blocked = seq
            break
        if type(decision) is NoWakeDecision:
            no_wake.append(seq)
            covered = seq
            continue
        if type(decision) is not WakeDecision:
            raise IdentityConflict("unsupported frozen wake decision")
        claims = [
            claim
            for claim in receipt.claims
            if claim.recipient_lookup == recipient_lookup
            and claim.recipient == recipient.canonical_thread
        ]
        if len(claims) != 1 or claims[0].wake_mode is not decision.wake_mode:
            raise IdentityConflict("selected claim differs from frozen bus recipient")
        if decision.wake_mode is WakeMode.PASSIVE:
            blocked = seq  # PASSIVE has no native injection semantics.
            break
        evidence = read_historical_native_inputs(
            store,
            wire_root_id=wire_root_id,
            recipient_lookup=recipient_lookup,
            source_seq=seq,
        )
        stages = {proof.stage: proof for proof in evidence}
        needed = (
            ("full",)
            if decision.wake_mode is WakeMode.FULL
            else (
                ("triage", "full")
                if stages.get("triage") and stages["triage"].triage_result == "full"
                else ("triage",)
            )
        )
        if (
            any(
                stage not in stages or not stages[stage].expected_prompt_equality_established
                for stage in needed
            )
            or (
                decision.wake_mode is WakeMode.BOUNDED_TRIAGE
                and (
                    "triage" not in stages
                    or stages["triage"].triage_result not in {"ignore", "full"}
                )
            )
            or any(proof.claim_id != claims[0].claim_id for proof in evidence)
        ):
            blocked = seq
            break
        injected.append(seq)
        covered = seq
    return ProvenSourceCoverage(
        wire_root_id, recipient_lookup, covered, tuple(injected), tuple(no_wake), blocked
    )
