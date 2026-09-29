"""Bounded, read-only historical coverage of private, frozen delivery sources.

This is NOT an inbox ACK or a work-selection cursor. In particular, an input
with UNKNOWN native outcome is a barrier, even when a later source has proof.
No missing claim is accepted, no native input is recovered/retried, and a
historical watermark grants no current-owner, provider, write or reply permit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination

from .cohort_schema import ClaimBatchReceipts, assert_cohort_schema
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_cohort import _receipt_matches
from .historical_native_inputs import read_historical_native_inputs
from .message_bus import MessageBus
from .private_bus_checkpoint import PrefixWitness, certified_initial_page_unlocked
from .wake import NoWakeDecision, WakeDecision

_MAX_SCAN_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class ProvenSourceCoverage:
    wire_root_id: str
    recipient_lookup: str
    covered_seq: int
    injected_source_seqs: tuple[int, ...]
    no_wake_seqs: tuple[int, ...]
    blocked_seq: int | None
    more_initials: bool = False
    source_witness: PrefixWitness = field(kw_only=True)


def read_proven_source_coverage(
    bus: MessageBus,
    store: Coordination,
    *,
    wire_root_id: str,
    recipient_lookup: str,
    limit: int = 100,
    after_seq: int = 0,
    partial: bool = False,
) -> ProvenSourceCoverage:
    """Conservatively walk a bounded, *canonical* private initial snapshot.

    ``covered_seq`` means every initial in the snapshot through that sequence
    was either not addressed to this stable lookup, explicitly no-wake with a
    sealed N receipt, or has corroborated live-recorded native evidence for
    the necessary selected stage(s). It is not an injected-message cursor:
    no-wake and absent-audience rows are not injections. A selected unproven
    source stops the walk even if a later source is independently proven.
    The canonical certificate bounds each addressed-source page. A partial
    page follows a previously verified prefix; more_initials never attests
    coverage of later sources. An exhausted page uses the sealed latest initial
    sequence, never the global bus high-water (which includes replies).
    Filesystem fsync/locks are not a hard wall-clock deadline.
    """
    if (
        type(bus) is not MessageBus
        or type(store) is not Coordination
        or type(wire_root_id) is not str
        or len(wire_root_id) != 32
        or any(ch not in "0123456789abcdef" for ch in wire_root_id)
        or type(recipient_lookup) is not str
        or len(recipient_lookup) != 32
        or any(ch not in "0123456789abcdef" for ch in recipient_lookup)
        or type(limit) is not int
        or not 0 < limit <= 100
        or type(after_seq) is not int
        or after_seq < 0
        or type(partial) is not bool
        or (after_seq != 0 and not partial)
    ):
        raise ValueError("source coverage needs exact private identities and bounded scan")
    if store.session._connection.in_transaction:
        raise IdentityConflict("source coverage requires a committed coordinator snapshot")
    deadline = time.monotonic() + _MAX_SCAN_SECONDS
    # This source certificate is not a selected claim, native proof or ACK.
    with bus.log.locked(blocking=False):
        if time.monotonic() > deadline:
            raise IdentityConflict("source coverage exceeded its scan deadline")
        marker = bus.log._private_marker_unlocked()
        if marker.root_id != wire_root_id:
            raise IdentityConflict("source coverage private wire root changed")
        source_witness, initials, more_initials = certified_initial_page_unlocked(
            bus.log, marker, recipient_lookup, after=after_seq, limit=limit
        )
        if after_seq > max(source_witness.latest_initial_seq, marker.admission_after_seq):
            raise IdentityConflict("source coverage prefix exceeds certified initials")
        if more_initials and not partial:
            raise IdentityConflict("source coverage exceeded its bounded private initial scan")
        horizon = initials[-1].message.seq if more_initials else source_witness.latest_initial_seq
    covered = after_seq if partial else 0
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
        with store.session.read():
            assert_cohort_schema(store.session._connection)
            assert_native_runtime_schema(store.session._connection)
            sealed = ClaimBatchReceipts.one(
                store.session._connection,
                wire_root_id=wire_root_id,
                wire_seq=seq,
            )
            receipt = (
                _receipt_matches(store.session._connection, initial) if sealed and sealed.sealed else None
            )
        if receipt is None:
            blocked = seq
            break
        if type(decision) is NoWakeDecision:
            no_wake.append(seq)
            covered = seq
            continue
        if type(decision) is not WakeDecision:
            raise IdentityConflict("unsupported frozen wake decision")
        assignments = [
            assignment
            for assignment in receipt.assignments
            if assignment.recipient_lookup == recipient_lookup
            and assignment.recipient == recipient.canonical_thread
        ]
        if len(assignments) != 1 or assignments[0].lifecycle.mode != decision.wake_mode:
            raise IdentityConflict("selected claim differs from frozen bus recipient")
        if not decision.wake_mode.active:
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
            if decision.wake_mode.active and not decision.wake_mode.triage
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
                decision.wake_mode.triage
                and (
                    "triage" not in stages
                    or stages["triage"].triage_result not in {"ignore", "full"}
                )
            )
            or any(proof.assignment_id != assignments[0].assignment_id for proof in evidence)
        ):
            blocked = seq
            break
        injected.append(seq)
        covered = seq
    if blocked is None:
        # The certified addressed page is exhaustive to this source bound.
        # Absent recipients are covered, but they are NEVER native injections.
        covered = max(covered, horizon)
    return ProvenSourceCoverage(
        wire_root_id,
        recipient_lookup,
        covered,
        tuple(injected),
        tuple(no_wake),
        blocked,
        more_initials,
        source_witness=source_witness,
    )
