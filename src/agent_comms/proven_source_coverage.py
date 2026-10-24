"""Bounded, read-only historical coverage of private, frozen delivery sources.

This is NOT an inbox ACK or a work-selection cursor. In particular, an input
with UNKNOWN native outcome is a barrier, even when a later source has proof.
No missing claim is accepted, no native input is recovered/retried, and a
historical watermark grants no current-owner, provider, write or reply permit.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination
from agent_comms.lifecycle_capability import Active
from agent_comms.native_runtime_input import NativeRuntimeTable

from .audience_manifest import FrozenRecipient
from .bus import Bus
from .bus_publication import Delivered
from .bus_source_page import CoveragePage
from .cohort_schema import ClaimBatchAcknowledgements, CohortTable
from .coordination_cohort import AcceptedCohort, _claim_record
from .coordination_session import NativeBoundary
from .field_codec import FieldCodec, RootId, ThreadLookup
from .historical_native_inputs import HistoricalNativeInput, read_historical_native_inputs
from .message_bus import MessageBus
from .native_input_record import NumberNativeColumn, TextNativeColumn, UnrecordedNativeColumn
from .native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from .private_path import FileIdentity, ObservedPath, PresentPath
from .source_proof_requirement import SourceSettlement
from .wake_policy import RecipientDecision

if TYPE_CHECKING:
    from .cursor_owner import CursorOwner, CursorState
    from .native_entries import NativeEvidenceScope
    from .private_registry_guard import RegistryPrivacy


@dataclass(frozen=True)
class SourcePrefix(PresentPath):
    """One immutable SQLite prefix; identity is not a message position."""

    through_seq: int

    @classmethod
    def capture(cls, bus: Bus, after_seq: int) -> SourcePrefix:
        identity = FileIdentity.from_stat(bus.path.stat())
        prefix = cls(identity, bus.revision())
        prefix.require_start(after_seq)
        prefix.require_current(bus)
        return prefix

    def require_start(self, covered_seq: int) -> None:
        if covered_seq > self.through_seq:
            raise IdentityConflict("current source coverage exceeds the bus")

    def require_current(self, bus: Bus) -> None:
        if not ObservedPath.at(bus.path).same_file(self.identity):
            raise IdentityConflict("native source database changed")
        if bus.revision() < self.through_seq:
            raise IdentityConflict("native source prefix regressed")


class CoverageInput(ABC):
    """The new suffix either injects one native reference or preserves the old one."""

    @abstractmethod
    def injected_for(self, state: CursorState) -> int: ...

    @abstractmethod
    def candidate(
        self, state: CursorState, owner: CursorOwner, coverage: ProvenSourceCoverage,
    ) -> CurrentNativeCursor: ...

    @abstractmethod
    def allows(self, owner: CursorOwner, db: sqlite3.Connection, state: CursorState) -> bool: ...

    @abstractmethod
    def require_reference(self, owner: CursorOwner, cursor: CurrentNativeCursor) -> None: ...

    def build(self, owner: CursorOwner, covered_seq: int) -> CurrentNativeCursor:
        return CurrentNativeCursor(
            wire_root_id=owner.wire_root_id, recipient_lookup=owner.lookup,
             owner_generation=owner.generation,
            owner_claimed_generation=owner.claimed_generation, covered_seq=covered_seq,
            **self.input_fields(),
        )

    @abstractmethod
    def input_fields(self) -> dict: ...


@dataclass(frozen=True)
class NoInjectedInput(CoverageInput):
    def input_fields(self) -> dict:
        return dict(injected_seq=0, input_id=UnrecordedNativeColumn(), stage=UnrecordedNativeColumn(),
                    session_id=UnrecordedNativeColumn(), request_generation=UnrecordedNativeColumn())

    def injected_for(self, state: CursorState) -> int:
        return state.injected_seq

    def candidate(
        self, state: CursorState, owner: CursorOwner, coverage: ProvenSourceCoverage,
    ) -> CurrentNativeCursor:
        return state.without_input(self, owner, coverage)

    def allows(self, owner: CursorOwner, db: sqlite3.Connection, state: CursorState) -> bool:
        return True

    def require_reference(self, owner: CursorOwner, cursor: CurrentNativeCursor) -> None:
        """A suffix without an injected input has no new reference to compare."""


@dataclass(frozen=True)
class InjectedInput(CoverageInput):
    original: HistoricalNativeInput

    def input_fields(self) -> dict:
        original = self.original
        return dict(
            injected_seq=original.source_seq, input_id=TextNativeColumn(original.input_id),
            stage=TextNativeColumn(FieldCodec.encode(type(original.execution))),
            session_id=TextNativeColumn(original.context.session_id),
            request_generation=NumberNativeColumn(original.context.request_generation),
        )
    def injected_for(self, state: CursorState) -> int:
        return self.original.source_seq

    def candidate(
        self, state: CursorState, owner: CursorOwner, coverage: ProvenSourceCoverage,
    ) -> CurrentNativeCursor:
        return self.build(owner, coverage.covered_seq)

    def allows(self, owner: CursorOwner, db: sqlite3.Connection, state: CursorState) -> bool:
        if self.original.owner_identity != owner.participant_identity:
            raise IdentityConflict('current cursor native proof belongs to another owner')
        state.require_input_progress(self.original)
        if not owner.permits_input(self.original.input_id):
            return False
        row = NativeRuntimeInput.one(db, input_id=self.original.input_id)
        if row is not None and not row.sent_owner_claimed_generation.matches(owner.claimed_generation):
            return state.claim_mismatch(self.original.source_seq)
        owner.require_recorded_reference(db, self.original.reference)
        return True

    def require_reference(self, owner: CursorOwner, cursor: CurrentNativeCursor) -> None:
        if cursor.injected_seq == self.original.source_seq and (
            cursor.reference != self.original.reference or self.original.owner_identity != owner.participant_identity
        ):
            raise IdentityConflict('current native cursor proof differs from journal')


@dataclass(frozen=True, slots=True)
class ProvenSourceCoverage:
    """One bounded source read and its corroborated original native inputs.

    This detached result lives only for the caller's read/publication operation.
    SQL identity, wire witness and live owner checks remain with the cursor;
    these historical observations never permit new input or replay.
    """

    wire_root_id: RootId
    recipient_lookup: ThreadLookup
    covered_seq: int
    inputs: tuple[HistoricalNativeInput, ...]
    no_wake_seqs: tuple[int, ...]
    blocked_seq: int | None
    more_sources: bool = False
    source_prefix: SourcePrefix = field(kw_only=True)

    @property
    def injected_source_seqs(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys(item.source_seq for item in self.inputs))

    @property
    def proof(self) -> CoverageInput:
        sequences = self.injected_source_seqs
        if not sequences:
            return NoInjectedInput()
        source_seq = sequences[-1]
        matching = [item for item in self.inputs
                    if item.source_seq == source_seq and item.expected_prompt_equality_established]
        if not matching:
            raise IdentityConflict("current cursor source lacks live-bound native proof")
        return InjectedInput(matching[-1])

    def evidence(self, *, through_seq: int | None = None) -> tuple[HistoricalNativeInput, ...]:
        """Consume the native inputs already corroborated in this source read."""
        if through_seq is None:
            return self.inputs
        return tuple(item for item in self.inputs if item.source_seq <= through_seq)


class SourceCoverage:
    """Canonical SQLite prefix reader bound to one stable recipient.

    Owns bounded paging and historical checks, never permission to start native input.
    No claim, input or schema is created by this read path.
    """

    page_budget = 32  # Existing bounded pass: 3,200 addressed initials.

    def __init__(
        self, bus: MessageBus, store: Coordination, *,
        wire_root_id: RootId, recipient_lookup: ThreadLookup,
    ):
        self.bus, self.store = bus, store
        self.wire_root_id = wire_root_id
        self.recipient_lookup = recipient_lookup

    def read(
        self, *, limit: int = 100, after_seq: int = 0, partial: bool = False,
        source_reads: NativeEvidenceScope,
    ) -> ProvenSourceCoverage:
        """Check a page; stop at selected UNKNOWN input or missing evidence.

        Count messages with no wake or no recipients without submitting input.
        At the end of a page, use the last checked message for this recipient,
        not the last message anywhere on the bus. The page is bounded by its row
        limit; elapsed time never decides whether it is readable.
        """
        request = CoveragePage.capture(lookup=self.recipient_lookup, limit=limit,
                                       after_seq=after_seq, partial=partial)
        return self._read(
            request, SourcePrefix.capture(self.bus.bus, request.after_seq), source_reads)

    def _read(
        self, request: CoveragePage, prefix: SourcePrefix, source_reads: NativeEvidenceScope,
    ) -> ProvenSourceCoverage:
        with source_reads.borrow(), NativeBoundary(self.bus) as boundary:
            boundary.privacy.require_root(self.wire_root_id, IdentityConflict("Coverage native namespace changed"))
            if self.store.session._connection.in_transaction:
                raise IdentityConflict("source coverage requires a committed coordinator snapshot")
            prefix.require_start(request.after_seq)
            records = self.bus.bus.received(
                self.recipient_lookup, after=request.after_seq, before=prefix.through_seq + 1,
                descending=False, limit=request.limit + 1,
            )
            more = len(records) > request.limit
            request.require_exhausted(more)
            initials = tuple(delivery for record in records[:request.limit]
                             for delivery in record.deliveries())
            prefix.require_current(self.bus.bus)
            horizon = initials[-1].seq if more else prefix.through_seq
            covered, inputs, no_wake, blocked = request.after_seq if request.partial else 0, [], [], None
            for initial in initials:
                seq = initial.message.seq
                matches = [
                    (recipient, decision)
                    for recipient, decision in zip(
                        initial.recipients, initial.decisions, strict=True
                    )
                    if recipient.recipient_lookup == self.recipient_lookup
                ]
                if len(matches) > 1:
                    raise IdentityConflict("duplicate stable lookup in frozen audience")
                if not matches:
                    raise IdentityConflict("recipient source differs from its frozen audience")
                recipient, decision = matches[0]
                acknowledgement = self._accepted_cohort(initial, boundary.privacy)
                if acknowledgement is None:
                    blocked = seq
                    break
                claims = decision.claim_decisions()
                if not claims:
                    no_wake.append(seq)
                for selected in claims:
                    settlement = self._selected_inputs(seq, recipient, selected, acknowledgement, source_reads)
                    if not settlement.apply(inputs):
                        blocked = seq
                        break
                if blocked is not None:
                    break
                covered = seq
            if blocked is None:
                covered = max(covered, horizon)
            prefix.require_current(self.bus.bus)
            return ProvenSourceCoverage(
                self.wire_root_id,
                self.recipient_lookup,
                covered,
                tuple(inputs),
                tuple(no_wake),
                blocked,
                more,
                source_prefix=prefix,
            )

    def _accepted_cohort(self, initial: Delivered, privacy: RegistryPrivacy) -> AcceptedCohort | None:
        with self.store.session.read():
            db = self.store.session._connection
            CohortTable.require_installed(db)
            NativeRuntimeTable.require_installed(db)
            sealed = ClaimBatchAcknowledgements.one(
                db, wire_root_id=self.wire_root_id, wire_seq=initial.message.seq
            )
            return _claim_record(db, initial, privacy) if sealed and sealed.sealed else None

    def _selected_inputs(
        self, seq: int, recipient: FrozenRecipient, decision: RecipientDecision, acknowledgement: AcceptedCohort,
        source_reads: NativeEvidenceScope,
    ) -> SourceSettlement:
        assignments = [
            a
            for a in acknowledgement.assignments
            if a.recipient_lookup == self.recipient_lookup
            and a.recipient == recipient.canonical_thread
        ]
        policy = decision.wake_policy()
        if len(assignments) != 1 or assignments[0].lifecycle.mode != policy:
            raise IdentityConflict("selected claim differs from frozen bus recipient")
        evidence = (self.native_inputs(seq, source_reads=source_reads)
                    if policy.items_with(Active, policy) else ())
        assignment = assignments[0]
        return assignment.lifecycle.source_settlement(evidence, policy, assignment.assignment_id)

    def native_inputs(self, source_seq: int, *, source_reads: NativeEvidenceScope) -> tuple[HistoricalNativeInput, ...]:
        return read_historical_native_inputs(
            self.store,
            wire_root_id=self.wire_root_id,
            recipient_lookup=self.recipient_lookup,
            source_seq=source_seq, source_reads=source_reads,
        )

    def since(self, state: CursorState, *,
              source_reads: NativeEvidenceScope) -> ProvenSourceCoverage:
        """Prove sources beyond the cursor, bounded to one captured SQLite prefix."""
        with source_reads.borrow():
            prefix = SourcePrefix.capture(self.bus.bus, state.covered_seq)
            covered = state.covered_seq
            inputs, no_wake = [], []
            for _ in range(self.page_budget):
                request = CoveragePage.capture(lookup=self.recipient_lookup,
                                              after_seq=covered, partial=True)
                page = self._read(request, prefix, source_reads)
                if page.covered_seq < covered:
                    raise IdentityConflict("canonical source coverage regressed between pages")
                covered = page.covered_seq
                inputs.extend(page.inputs)
                no_wake.extend(page.no_wake_seqs)
                if (
                    page.blocked_seq is not None
                    or not page.more_sources
                ):
                    return ProvenSourceCoverage(
                        self.wire_root_id,
                        self.recipient_lookup,
                        covered,
                        tuple(inputs),
                        tuple(no_wake),
                        page.blocked_seq,
                        page.more_sources,
                        source_prefix=prefix,
                    )
            raise IdentityConflict("source coverage exceeded bounded canonical page budget")
