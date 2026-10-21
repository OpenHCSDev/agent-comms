"""Captured owner and typed initial/recorded states for native source cursors."""
from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

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
from .private_send_rules import RunningTurnRegistryCheck

if TYPE_CHECKING:
    from .acp_extension import CursorObservation
    from .proven_source_coverage import ProvenSourceCoverage


@dataclass(frozen=True)
class CursorOwner(RegistryOwner):
    """Record this started input; deciding whether tools may run belongs to its turn."""

    check_type = RunningTurnRegistryCheck
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
            db, wire_root_id=self.wire_root_id, recipient_lookup=self.lookup,
            owner_generation=self.generation, owner_claimed_generation=self.claimed_generation,
        )

    def state(self, db: sqlite3.Connection) -> CursorState:
        """Classify recipient progress once, at its storage boundary."""
        row = self.cursor(db)
        if row is None:
            return UnstartedCursor()
        self.require_recorded_input(db, row)
        return RecordedCursor(row)

    def _matches_input(self, row, reference) -> bool:
        return (row is not None and row.owner_identity == self.participant_identity
                and row.sent_owner_claimed_generation.matches(self.claimed_generation)
                and row.reference == reference)

    def matches_prefix(self, db, evidence) -> bool:
        return all(self._matches_input(NativeRuntimeInput.one(db, input_id=item.input_id), item.reference)
                   for item in evidence)

    def require_recorded_input(self, db, cursor) -> None:
        cursor.reference.require_recorded_input(self, db)

    def require_coverage(self, db, cursor, coverage) -> None:
        if cursor.owner_identity != self.participant_identity:
            raise IdentityConflict("current native cursor owner identity differs")
        if cursor.covered_seq > coverage.covered_seq:
            raise IdentityConflict("current native cursor exceeds canonical source proof")
        if not self.matches_prefix(db, coverage.evidence(through_seq=cursor.covered_seq)):
            raise IdentityConflict("current cursor borrows historical owner source proof")
        self.require_recorded_input(db, cursor)

    def require_recorded_reference(self, db, reference: NativeInputReference) -> None:
        row = NativeRuntimeInput.one(db, input_id=reference.input_id)
        if row is None or not row.sent_owner_claimed_generation.matches(self.claimed_generation):
            raise IdentityConflict("current cursor input claim_scope differs")
        if not self._matches_input(row, reference):
            raise IdentityConflict("current cursor proof differs from journal")


class CursorState(ABC):
    """One SQL state owns its scan start, checks and publication behavior."""

    @property
    @abstractmethod
    def covered_seq(self) -> int: ...

    @property
    @abstractmethod
    def cursor(self) -> CurrentNativeCursor | None: ...

    @property
    @abstractmethod
    def injected_seq(self) -> int: ...

    @abstractmethod
    def without_input(self, proof, owner, coverage): ...

    def require_progress(self, owner, coverage, injected) -> None:
        if coverage.covered_seq < self.covered_seq:
            raise IdentityConflict("current cursor would regress")

    @abstractmethod
    def observation(self) -> CursorObservation: ...

    def proves_input(self, input_id: str) -> bool:
        return False

    def require_input_progress(self, proof) -> None:
        pass

    def claim_mismatch(self, injected_seq) -> bool:
        return False

    @abstractmethod
    def publish(self, db, owner, coverage, proof) -> CursorState: ...

    @abstractmethod
    def require_coverage(self, owner, db, coverage, proof) -> None: ...


@dataclass(frozen=True)
class UnstartedCursor(CursorState):
    @property
    def covered_seq(self):
        return 0

    @property
    def cursor(self):
        return None

    @property
    def injected_seq(self):
        return 0  # No injected native input; cursor presence is this state's type.

    def without_input(self, proof, owner, coverage):
        return proof.build(owner, coverage.covered_seq)

    def observation(self):
        from .acp_extension import EmptyCursorObservation

        return EmptyCursorObservation()

    def publish(self, db, owner, coverage, proof):
        if coverage.covered_seq <= self.covered_seq:
            return self  # A blocked first source has not established a cursor.
        cursor = proof.candidate(self, owner, coverage)
        cursor.insert(db)
        return RecordedCursor(cursor)

    def require_coverage(self, owner, db, coverage, proof):
        pass  # No durable cursor exists to assert coverage.


@dataclass(frozen=True)
class RecordedCursor(CursorState):
    row: CurrentNativeCursor

    @property
    def covered_seq(self):
        return self.row.covered_seq

    @property
    def cursor(self):
        return self.row

    @property
    def injected_seq(self):
        return self.row.injected_seq

    def without_input(self, proof, owner, coverage):
        return replace(self.row, covered_seq=coverage.covered_seq)

    def observation(self):
        from .acp_extension import VerifiedCursorObservation

        return VerifiedCursorObservation(self.row)

    def proves_input(self, input_id: str) -> bool:
        return self.row.input_id.holds(input_id)

    def require_progress(self, owner, coverage, injected):
        super().require_progress(owner, coverage, injected)
        if (self.row.owner_identity != owner.participant_identity
                or injected < self.row.injected_seq):
            raise IdentityConflict("current cursor would change owner or regress")

    def require_input_progress(self, proof):
        if proof.source_seq <= self.row.injected_seq and not self.proves_input(proof.input_id):
            raise IdentityConflict("current cursor native input changed")

    def claim_mismatch(self, injected_seq):
        if injected_seq > self.row.injected_seq:
            return False
        raise IdentityConflict("current cursor input claim_scope differs")

    def publish(self, db, owner, coverage, proof):
        cursor = proof.candidate(self, owner, coverage)
        if cursor.covered_seq == self.row.covered_seq and cursor.injected_seq == self.row.injected_seq:
            return self
        updated = CurrentNativeCursor.update(
            db, where="wire_root_id=? AND recipient_lookup=? AND owner_generation=? "
            "AND {owner_claimed_generation}=? AND covered_seq=? AND injected_seq=?",
            parameters=(owner.wire_root_id, owner.lookup, owner.generation, owner.claimed_generation,
                        self.row.covered_seq, self.row.injected_seq),
            covered_seq=cursor.covered_seq, injected_seq=cursor.injected_seq,
            input_id=cursor.input_id, stage=cursor.stage, session_id=cursor.session_id,
            request_generation=cursor.request_generation,
        )
        if updated.rowcount != 1:
            raise StaleFence("current cursor monotonic update lost its fence")
        return RecordedCursor(cursor)

    def require_coverage(self, owner, db, coverage, proof):
        owner.require_coverage(db, self.row, coverage)
        proof.require_reference(owner, self.row)
