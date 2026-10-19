"""One captured live owner identity for source-cursor read and publication fences."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_errors import IdentityConflict, StaleFence
from .coordinator import Coordination
from .historical_native_inputs import HistoricalNativeInput
from .message_bus import MessageBus
from .native_input_owner import RegistryOwner, ParticipantOwner
from .native_input_record import NativeInputReference, UnrecordedNativeInputReference
from .thread_identity import GenerationCounter
from .native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from .registry_document import RegistrySnapshot

if TYPE_CHECKING:
    from .proven_source_coverage import ProvenSourceCoverage


@dataclass(frozen=True)
class CursorOwner(RegistryOwner):
    """A captured identity; recheck it under locks before permitting work."""

    wire_root_id: str
    generation: int

    def __post_init__(self):
        GenerationCounter.require_positive(self.generation)
        GenerationCounter.require_positive(self.claimed_generation)

    @property
    def participant_identity(self):
        return ParticipantOwner(self.thread, self.generation).coordinator_identity(self.lookup)

    @property
    def lookup(self) -> str:
        return self.thread.incarnation.lookup

    def require_live(self, bus: MessageBus, registry: RegistrySnapshot, reason: str) -> None:
        if bus.log.private_marker_unlocked().root_id != self.wire_root_id:
            raise StaleFence(reason)
        self.require_snapshot(registry, reason)

    def require_participant(self, store: Coordination, reason: str) -> None:
        assert_native_runtime_schema(store.session._connection)
        person = store.participants.get(self.lookup)
        if not person.committed or person.owner_identity != self.participant_identity:
            raise StaleFence(reason)

    def cursor(self, db: sqlite3.Connection) -> CurrentNativeCursor | None:
        return CurrentNativeCursor.one(
            db,
            wire_root_id=self.wire_root_id,
            recipient_lookup=self.lookup,
            owner_generation=self.generation,
            owner_claimed_generation=self.claimed_generation,
        )

    def _matches_input(self, row: NativeRuntimeInput | None, reference: NativeInputReference) -> bool:
        if row is None or row.owner_identity != self.participant_identity:
            return False
        return row.sent_owner_claimed_generation.matches(self.claimed_generation) and row.reference == reference

    def matches_prefix(
        self, db: sqlite3.Connection, evidence: tuple[HistoricalNativeInput, ...]
    ) -> bool:
        return all(
            self._matches_input(
                NativeRuntimeInput.one(db, input_id=item.input_id),
                item.reference,
            )
            for item in evidence
        )

    def require_recorded_input(self, db: sqlite3.Connection, cursor: CurrentNativeCursor) -> None:
        cursor.reference.require_recorded_input(self, db)

    def require_coverage(
        self, db: sqlite3.Connection, cursor: CurrentNativeCursor,
        coverage: ProvenSourceCoverage, *, prior: CurrentNativeCursor | None = None,
    ) -> None:
        """Validate the exact SQL row against this operation's original proof.

        A declined advancement may retain an earlier row. Its covered prefix
        still needs its own input permission and reference checks; later evidence cannot
        turn that old row into a verified observation.
        """
        if cursor.owner_identity != self.participant_identity:
            raise IdentityConflict("current native cursor owner identity differs")
        if cursor.covered_seq > coverage.covered_seq:
            raise IdentityConflict("current native cursor exceeds canonical source proof")
        if not self.matches_prefix(db, coverage.evidence(through_seq=cursor.covered_seq)):
            raise IdentityConflict("current cursor borrows historical owner source proof")
        if prior is not None and cursor.injected_seq == prior.injected_seq:
            if (prior.owner_identity != self.participant_identity
                or cursor.covered_seq < prior.covered_seq
                or cursor.reference != prior.reference):
                raise IdentityConflict("current native cursor regresses its durable proof")
        else:
            proof = coverage.last_proof(cursor.injected_seq)
            expected = proof.reference if proof is not None else UnrecordedNativeInputReference()
            if cursor.reference != expected:
                raise IdentityConflict("current native cursor proof differs from journal")
            if proof is not None and proof.owner_identity != self.participant_identity:
                raise IdentityConflict("current native cursor proof belongs to another owner")
        self.require_recorded_input(db, cursor)

    def require_recorded_reference(self, db: sqlite3.Connection, reference: NativeInputReference) -> None:
        row = NativeRuntimeInput.one(db, input_id=reference.input_id)
        if row is None or not row.sent_owner_claimed_generation.matches(self.claimed_generation):
            raise IdentityConflict("current cursor input claim_scope differs")
        if not self._matches_input(
            row,
            reference,
        ):
            raise IdentityConflict("current cursor proof differs from journal")

    def allows(
        self,
        db: sqlite3.Connection,
        proof: HistoricalNativeInput | None,
        injected_seq: int,
        prior: CurrentNativeCursor | None,
        committed_input_id: str | None,
    ) -> bool:
        """The recorded input identifies only its own permitted live operation.

        A settled caller may additionally fence the expected input ID. Idle
        projection uses the same immutable acknowledgement, not a cached completion ID;
        absence of that caller assertion does not erase the recorded input permission.
        """
        if proof is None:
            return True  # Covered no-wake prefix may precede an UNKNOWN gap.
        if proof.owner_identity != self.participant_identity or proof.source_seq != injected_seq:
            raise IdentityConflict("current cursor native proof belongs to another owner")
        if (
            prior is not None
            and injected_seq <= prior.injected_seq
            and prior.input_id != proof.input_id
        ):
            raise IdentityConflict("current cursor native input changed")
        if committed_input_id is not None and proof.input_id != committed_input_id:
            return False
        reserved = NativeRuntimeInput.one(db, input_id=proof.input_id)
        if (
            reserved is not None
            and not reserved.sent_owner_claimed_generation.matches(self.claimed_generation)
        ):
            if prior is None or injected_seq > prior.injected_seq:
                return False
            raise IdentityConflict("current cursor input claim_scope differs")
        if not self._matches_input(
            reserved,
            proof.reference,
        ):
            raise IdentityConflict("current cursor differs from committed native acknowledgement")
        return True
