"""One captured live owner identity for source-cursor read and publication fences."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .bus_publication import stable_thread_lookup
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_errors import IdentityConflict, StaleFence
from .coordinator import Coordination
from .historical_native_inputs import HistoricalNativeInput
from .message_bus import MessageBus
from .native_input_owner import RegistryOwner, ParticipantOwner
from .native_input_record import NativeInputReference
from .thread_identity import GenerationCounter
from .native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from .registry_document import RegistrySnapshot


@dataclass(frozen=True)
class CursorOwner(RegistryOwner):
    """A captured identity, never an admission permit until rechecked under locks."""

    wire_root_id: str
    generation: int

    def __post_init__(self):
        GenerationCounter.require_positive(self.generation)
        GenerationCounter.require_positive(self.admission_generation)

    @property
    def participant_identity(self):
        return ParticipantOwner(self.thread, self.generation).coordinator_identity(self.lookup)

    @property
    def lookup(self) -> str:
        return stable_thread_lookup(self.thread.created_at)

    def require_live(self, bus: MessageBus, registry: RegistrySnapshot, reason: str) -> None:
        if bus.log._private_marker_unlocked().root_id != self.wire_root_id:
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
            owner_admission_generation=self.admission_generation,
        )

    def _matches_input(self, row: NativeRuntimeInput | None, reference: NativeInputReference) -> bool:
        if row is None or row.owner_identity != self.participant_identity:
            return False
        return row.sent_owner_admission_generation.matches(self.admission_generation) and row.reference == reference

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

    def require_recorded_reference(self, db: sqlite3.Connection, reference: NativeInputReference) -> None:
        row = NativeRuntimeInput.one(db, input_id=reference.input_id)
        if row is None or not row.sent_owner_admission_generation.matches(self.admission_generation):
            raise IdentityConflict("current cursor input admission differs")
        if not self._matches_input(
            row,
            reference,
        ):
            raise IdentityConflict("current cursor proof differs from journal")

    def admits(
        self,
        db: sqlite3.Connection,
        proof: HistoricalNativeInput | None,
        injected_seq: int,
        prior: CurrentNativeCursor | None,
        committed_input_id: str | None,
    ) -> bool:
        """The original recorded input seeds only its own live admission.

        A settled caller may additionally fence the expected input ID. Idle
        projection uses the same immutable receipt, not a cached completion ID;
        absence of that caller assertion does not erase the receipt's admission.
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
            and not reserved.sent_owner_admission_generation.matches(self.admission_generation)
        ):
            if prior is None or injected_seq > prior.injected_seq:
                return False
            raise IdentityConflict("current cursor input admission differs")
        if not self._matches_input(
            reserved,
            proof.reference,
        ):
            raise IdentityConflict("current cursor differs from committed native receipt")
        return True
