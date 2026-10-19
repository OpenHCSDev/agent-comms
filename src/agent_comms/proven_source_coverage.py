"""Bounded, read-only historical coverage of private, frozen delivery sources.

This is NOT an inbox ACK or a work-selection cursor. In particular, an input
with UNKNOWN native outcome is a barrier, even when a later source has proof.
No missing claim is accepted, no native input is recovered/retried, and a
historical watermark grants no current-owner, provider, write or reply permit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Annotated, TYPE_CHECKING

from .bus_source_page import CoveragePage
from .field_codec import FieldCodec, Id128Text

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination

from .audience_manifest import FrozenRecipient
from .bus_publication import CommittedDelivery
from .cohort_schema import ClaimBatchAcknowledgements, assert_cohort_schema
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_cohort import AcceptedCohort, _claim_record
from .historical_native_inputs import HistoricalNativeInput, read_historical_native_inputs
from .message_bus import MessageBus
from .private_bus_checkpoint import (
    PrefixWitness,
    verify_private_bus_checkpoint_unlocked,
)
from .wake_policy import RecipientDecision

if TYPE_CHECKING:
    from .native_entries import NativeEvidenceScope
from .store_files import StoreLockContention

_MAX_SCAN_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class ProvenSourceCoverage:
    """One bounded source read and its corroborated original native inputs.

    This detached result lives only for the caller's read/publication operation.
    SQL identity, wire witness and live owner checks remain with the cursor;
    these historical observations never permit new input or replay.
    """

    wire_root_id: str
    recipient_lookup: str
    covered_seq: int
    inputs: tuple[HistoricalNativeInput, ...]
    no_wake_seqs: tuple[int, ...]
    blocked_seq: int | None
    more_sources: bool = False
    source_witness: PrefixWitness = field(kw_only=True)

    @property
    def injected_source_seqs(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys(item.source_seq for item in self.inputs))

    def evidence(self, *, through_seq: int | None = None) -> tuple[HistoricalNativeInput, ...]:
        """Consume the native inputs already corroborated in this source read."""
        if through_seq is None:
            return self.inputs
        return tuple(item for item in self.inputs if item.source_seq <= through_seq)

    def last_proof(self, source_seq: int) -> HistoricalNativeInput | None:
        if source_seq == 0:
            return None
        matching = [item for item in self.inputs
                    if item.source_seq == source_seq and item.expected_prompt_equality_established]
        if not matching:
            raise IdentityConflict("current cursor source lacks live-bound native proof")
        return matching[-1]


class SourceCoverage:
    """Canonical source snapshot reader bound to one wire and stable recipient.

    Owns bounded paging and historical checks, never permission to start native input.
    No claim, input or schema is created by this read path.
    """

    page_budget = 32  # Existing bounded pass: 3,200 addressed initials.

    def __init__(
        self, bus: MessageBus, store: Coordination, *, wire_root_id: str, recipient_lookup: str,
        contention: StoreLockContention | None = None,
    ):
        self.bus, self.store = bus, store
        self.contention = contention  # Borrowed lock wait resource; never source evidence.
        self.wire_root_id = FieldCodec.decode(Annotated[str, Id128Text], wire_root_id)
        self.recipient_lookup = FieldCodec.decode(Annotated[str, Id128Text], recipient_lookup)

    def witness_unlocked(self) -> PrefixWitness:
        return verify_private_bus_checkpoint_unlocked(
            self.bus.log, self.bus.log.private_marker_unlocked()
        )

    def witness(self) -> PrefixWitness:
        with self.bus.log.certified_read(
            blocking=False, contention=self.contention
        ) as source:
            return source.witness

    def read(
        self, *, limit: int = 100, after_seq: int = 0, partial: bool = False,
        source_reads: NativeEvidenceScope,
    ) -> ProvenSourceCoverage:
        """Check a page; stop at selected UNKNOWN input or missing evidence.

        Count messages with no wake or no recipients without submitting input.
        At the end of a page, use the last checked message for this recipient,
        not the last message anywhere on the bus. Filesystem flushes and locks
        can still take longer than the requested time limit.
        """
        with source_reads.borrow():
            request = CoveragePage.capture(lookup=self.recipient_lookup, limit=limit,
                                           after_seq=after_seq, partial=partial)
            if self.store.session._connection.in_transaction:
                raise IdentityConflict("source coverage requires a committed coordinator snapshot")
            witness, initials, more = self._page(request)
            horizon = initials[-1].message.seq if more else witness.latest_source_seq
            covered, inputs, no_wake, blocked = after_seq if partial else 0, [], [], None
            for initial in initials:
                seq = initial.message.seq
                matches = [
                    (recipient, decision)
                    for recipient, decision in zip(
                        initial.audience.recipients, initial.decisions, strict=True
                    )
                    if recipient.recipient_lookup == self.recipient_lookup
                ]
                if len(matches) > 1:
                    raise IdentityConflict("duplicate stable lookup in frozen audience")
                if not matches:
                    covered = seq
                    continue
                recipient, decision = matches[0]
                acknowledgement = self._accepted_cohort(initial)
                if acknowledgement is None:
                    blocked = seq
                    break
                claims = decision.claim_decisions()
                if not claims:
                    no_wake.append(seq)
                for selected in claims:
                    evidence = self._selected_inputs(seq, recipient, selected, acknowledgement, source_reads)
                    if not evidence:
                        blocked = seq
                        break
                    inputs.extend(evidence)
                if blocked is not None:
                    break
                covered = seq
            if blocked is None:
                covered = max(covered, horizon)
            return ProvenSourceCoverage(
                self.wire_root_id,
                self.recipient_lookup,
                covered,
                tuple(inputs),
                tuple(no_wake),
                blocked,
                more,
                source_witness=witness,
            )

    def _page(self, request: CoveragePage) -> tuple[PrefixWitness, tuple[CommittedDelivery, ...], bool]:
        deadline = time.monotonic() + _MAX_SCAN_SECONDS
        remaining_before = self.contention.remaining if self.contention is not None else 0.0
        with self.bus.log.certified_read(blocking=False, contention=self.contention) as source:
            waited = remaining_before - self.contention.remaining if self.contention is not None else 0.0
            if time.monotonic() > deadline + waited:
                raise IdentityConflict("source coverage exceeded its scan deadline")
            if source.marker.root_id != self.wire_root_id:
                raise IdentityConflict("source coverage private wire root changed")
            witness, initials, more = source.addressed_page(self.bus.log, request)
            if request.after_seq > max(witness.latest_source_seq, source.marker.claimed_after_seq):
                raise IdentityConflict("source coverage prefix exceeds certified initials")
            request.require_exhausted(more)
        return witness, tuple(initials), more

    def _accepted_cohort(self, initial: CommittedDelivery) -> AcceptedCohort | None:
        with self.store.session.read():
            db = self.store.session._connection
            assert_cohort_schema(db)
            assert_native_runtime_schema(db)
            sealed = ClaimBatchAcknowledgements.one(
                db, wire_root_id=self.wire_root_id, wire_seq=initial.message.seq
            )
            return _claim_record(db, initial) if sealed and sealed.sealed else None

    def _selected_inputs(
        self, seq: int, recipient: FrozenRecipient, decision: RecipientDecision, acknowledgement: AcceptedCohort,
        source_reads: NativeEvidenceScope,
    ) -> tuple[HistoricalNativeInput, ...]:
        assignments = [
            a
            for a in acknowledgement.assignments
            if a.recipient_lookup == self.recipient_lookup
            and a.recipient == recipient.canonical_thread
        ]
        policy = decision.wake_policy()
        if len(assignments) != 1 or assignments[0].lifecycle.mode != policy:
            raise IdentityConflict("selected claim differs from frozen bus recipient")
        if not policy.active:
            return ()  # PASSIVE is not native injection.
        evidence = self.native_inputs(seq, source_reads=source_reads)
        if policy.proves_source(evidence) and all(
            p.assignment_id == assignments[0].assignment_id for p in evidence
        ):
            return evidence
        return ()

    def native_inputs(self, source_seq: int, *, source_reads: NativeEvidenceScope) -> tuple[HistoricalNativeInput, ...]:
        return read_historical_native_inputs(
            self.store,
            wire_root_id=self.wire_root_id,
            recipient_lookup=self.recipient_lookup,
            source_seq=source_seq, source_reads=source_reads,
        )

    def prefix(self, *, through_seq: int | None = None,
               source_reads: NativeEvidenceScope) -> ProvenSourceCoverage:
        """Rescan the whole activation prefix; no persisted high-water is trusted."""
        with source_reads.borrow():
            with self.bus.log.certified_read(
                blocking=False, contention=self.contention
            ) as source:
                marker = source.marker
                if marker.root_id != self.wire_root_id:
                    raise IdentityConflict("current source claim_scope root changed")
                floor = marker.claimed_after_seq
            if through_seq is not None and 0 < through_seq <= floor:
                raise IdentityConflict("current source proof precedes this activation")
            covered, inputs, no_wake, witness = floor, [], [], None
            for _ in range(self.page_budget):
                page = self.read(after_seq=covered, partial=True, source_reads=source_reads)
                if page.covered_seq < covered:
                    raise IdentityConflict("canonical source coverage regressed between pages")
                if witness is not None and page.source_witness != witness:
                    raise IdentityConflict("certified source changed between coverage pages")
                witness, covered = page.source_witness, page.covered_seq
                inputs.extend(page.inputs)
                no_wake.extend(page.no_wake_seqs)
                if (
                    (through_seq is not None and covered >= through_seq)
                    or page.blocked_seq is not None
                    or not page.more_sources
                ):
                    return ProvenSourceCoverage(
                        self.wire_root_id,
                        self.recipient_lookup,
                        covered if covered > floor else 0,
                        tuple(inputs),
                        tuple(no_wake),
                        page.blocked_seq,
                        page.more_sources,
                        source_witness=witness,
                    )
            raise IdentityConflict("source coverage exceeded bounded canonical page budget")
