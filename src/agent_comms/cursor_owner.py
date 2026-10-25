"""Captured owner and typed initial/recorded states for native source cursors."""
from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from agent_comms.field_codec import InputId, RootId, ThreadLookup
from agent_comms.native_runtime_input import NativeRuntimeTable

from .acp_extension import CursorObservation, EmptyCursorObservation, VerifiedCursorObservation
from .coordination_database import CoordinationStore
from .coordination_errors import IdentityConflict, StaleFence
from .coordinator import Coordination
from .message_bus import MessageBus
from .native_input_owner import ParticipantOwner, RegistryOwner
from .native_input_record import NativeInputReference
from .native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from .private_send_rules import RunningTurnRegistryCheck
from .proven_source_coverage import SourceCoverage, SourcePrefix
from .registry_document import RegistrySnapshot
from .thread_identity import GenerationCounter

if TYPE_CHECKING:
    from sqlite3 import Connection

    from .coordination_tables.participants import OwnerGenerations
    from .historical_native_inputs import HistoricalNativeInput
    from .native_entries import NativeEvidenceScope
    from .proven_source_coverage import CoverageInput, NoInjectedInput, ProvenSourceCoverage


@dataclass(frozen=True)
class CursorOwner(RegistryOwner):
    """Record this started input; deciding whether tools may run belongs to its turn."""

    check_type = RunningTurnRegistryCheck
    wire_root_id: RootId
    generation: int

    def __post_init__(self):
        GenerationCounter.require_positive(self.generation)
        GenerationCounter.require_positive(self.claimed_generation)

    @classmethod
    async def read_async(cls, bus: MessageBus, *, wire_root_id: RootId, owner_name: str,
                         source_reads: NativeEvidenceScope) -> CursorState:
        return await Coordination.run_read_async(
            CoordinationStore.path_in(bus.bus.path.parent),
            partial(cls._read_owned, bus, wire_root_id, owner_name, source_reads),
        )

    @classmethod
    def _read_owned(
        cls, bus: MessageBus, root_id: RootId, owner_name: str,
        source_reads: NativeEvidenceScope, store: Coordination,
    ) -> CursorState:
        with store.session.boundary(bus) as boundary:
            reason = "Current native cursor has no matching live owner"
            bus._registry.store.private_guard_unlocked().require_root(root_id, StaleFence(reason))
            captured = RegistryOwner.capture(boundary.registry, owner_name, reason)
            with store.session.read():
                db = store.session._connection
                NativeRuntimeTable.require_installed(db)
                person = store.participants.get(
                    captured.thread.incarnation.require_recorded().lookup
                )
                if not person.committed:
                    raise StaleFence("Current cursor recipient is not committed")
                owner = cls(thread=captured.thread, claimed_generation=captured.claimed_generation,
                            wire_root_id=root_id, generation=person.owner_identity.generation)
                state = owner.state(db)
        with source_reads.borrow():
            prefix = SourcePrefix.capture(bus.bus, state.covered_seq)
        with store.session.boundary(bus) as boundary:
            prefix.require_current(bus.bus)
            owner.require_live(bus, boundary.registry, "Current cursor owner changed while reading")
            with store.session.read():
                owner.require_participant(store, "Current cursor participant generation changed")
                if owner.cursor(store.session._connection) != state.cursor:
                    raise StaleFence("Current cursor SQL row changed while reading")
        return state

    async def advance_async(
        self, bus: MessageBus, *, source_reads: NativeEvidenceScope,
    ) -> CursorState:
        return await Coordination.run_async(
            CoordinationStore.path_in(bus.bus.path.parent),
            partial(self._advance_owned, bus, source_reads),
        )

    def permits_input(self, input_id: InputId) -> bool:
        """A refresh can advance through any verified input for this owner."""
        return True

    @classmethod
    async def refresh_async(cls, bus: MessageBus, *, wire_root_id: RootId, owner_name: str,
                            source_reads: NativeEvidenceScope) -> CursorState:
        return await Coordination.run_async(
            CoordinationStore.path_in(bus.bus.path.parent),
            partial(cls._refresh_owned, bus, wire_root_id, owner_name, source_reads),
        )

    @classmethod
    def _refresh_owned(
        cls, bus: MessageBus, root_id: RootId, owner_name: str,
        source_reads: NativeEvidenceScope, store: Coordination,
    ) -> CursorState:
        thread, generation = bus._registry.live_owner_with_claim(owner_name)
        person = store.participants.get(thread.incarnation.require_recorded().lookup)
        owner = cls(thread=thread, claimed_generation=generation, wire_root_id=root_id,
                    generation=person.owner_identity.generation)
        return owner._advance_owned(bus, source_reads, store)

    def _advance_owned(self, bus: MessageBus, source_reads: NativeEvidenceScope, store: Coordination) -> CursorState:
        with source_reads.borrow():
            sources = SourceCoverage(bus, store, wire_root_id=self.wire_root_id,
                                     recipient_lookup=self.lookup)
            with store.session.boundary(bus), store.session.read():
                state = self.state(store.session._connection)
            coverage = sources.since(state, source_reads=source_reads)
            proof = coverage.proof
            with store.session.boundary(bus) as boundary, store.session.writing() as db:
                coverage.source_prefix.require_current(bus.bus)
                self.require_live(bus, boundary.registry, "Current cursor owner or namespace changed")
                self.require_participant(store, "Current cursor recipient generation changed")
                if self.cursor(db) != state.cursor:
                    raise StaleFence("Current cursor SQL row changed during source verification")
                state.require_progress(self, coverage, proof.injected_for(state))
                if self.matches_prefix(db, coverage.evidence()) and proof.allows(
                    self, db, state,
                ):
                    state = state.publish(db, self, coverage, proof)
                state.require_coverage(self, db, coverage, proof)
                return state

    @property
    def participant_identity(self) -> OwnerGenerations:
        return ParticipantOwner(self.thread, self.generation).coordinator_identity(self.lookup)

    @property
    def lookup(self) -> ThreadLookup:
        return self.thread.incarnation.require_recorded().lookup

    def require_live(self, bus: MessageBus, registry: RegistrySnapshot, reason: str) -> None:
        bus._registry.store.private_guard_unlocked().require_root(self.wire_root_id, StaleFence(reason))
        self.require_snapshot(registry, reason)

    def require_participant(self, store: Coordination, reason: str) -> None:
        NativeRuntimeTable.require_installed(store.session._connection)
        person = store.participants.get(self.lookup)
        if not person.committed or person.owner_identity != self.participant_identity:
            raise StaleFence(reason)

    def cursor(self, db: sqlite3.Connection) -> CurrentNativeCursor | None:
        """The thread's newest cursor; a restart's new claim continues it."""
        rows = CurrentNativeCursor.select(
            db, where="{owner_claimed_generation}<=?", parameters=(self.claimed_generation,),
            order_by=("owner_claimed_generation",), descending=True, limit=1,
            wire_root_id=self.wire_root_id, recipient_lookup=self.lookup, owner_generation=self.generation,
        )
        return next(iter(rows), None)

    def state(self, db: sqlite3.Connection) -> CursorState:
        """Classify recipient progress once, at its storage boundary."""
        row = self.cursor(db)
        if row is None:
            return UnstartedCursor()
        self.require_recorded_input(db, row)
        if row.owner_claimed_generation != self.claimed_generation:
            return InheritedCursor(row)
        return RecordedCursor(row)

    def _matches_input(self, row: NativeRuntimeInput | None, reference: NativeInputReference) -> bool:
        return (row is not None and row.owner_identity == self.participant_identity
                and row.sent_owner_claimed_generation.sent_by_lineage(self.claimed_generation)
                and row.reference == reference)

    def matches_prefix(self, db: Connection, evidence: tuple[HistoricalNativeInput, ...]) -> bool:
        return all(self._matches_input(NativeRuntimeInput.one(db, input_id=item.input_id), item.reference)
                   for item in evidence)

    def require_recorded_input(self, db: Connection, cursor: CurrentNativeCursor) -> None:
        cursor.reference.require_recorded_input(self, db)

    def require_coverage(self, db: Connection, cursor: CurrentNativeCursor, coverage: ProvenSourceCoverage) -> None:
        if cursor.owner_identity != self.participant_identity:
            raise IdentityConflict("current native cursor owner identity differs")
        if cursor.covered_seq > coverage.covered_seq:
            raise IdentityConflict("current native cursor exceeds canonical source proof")
        if not self.matches_prefix(db, coverage.evidence(through_seq=cursor.covered_seq)):
            raise IdentityConflict("current cursor borrows historical owner source proof")
        self.require_recorded_input(db, cursor)

    def require_recorded_reference(self, db: Connection, reference: NativeInputReference) -> None:
        row = NativeRuntimeInput.one(db, input_id=reference.input_id)
        if row is None or not row.sent_owner_claimed_generation.sent_by_lineage(self.claimed_generation):
            raise IdentityConflict("current cursor input claim_scope differs")
        if not self._matches_input(row, reference):
            raise IdentityConflict("current cursor proof differs from journal")


@dataclass(frozen=True)
class CommittedCursorOwner(CursorOwner):
    """Settlement advances only through this exact committed native input."""

    input_id: InputId

    def permits_input(self, input_id: InputId) -> bool:
        return self.input_id == input_id


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
    def without_input(self, proof: NoInjectedInput, owner: CursorOwner, coverage: ProvenSourceCoverage) -> CurrentNativeCursor: ...

    def require_progress(self, owner: CursorOwner, coverage: ProvenSourceCoverage, injected: int) -> None:
        if coverage.covered_seq < self.covered_seq:
            raise IdentityConflict("current cursor would regress")

    @abstractmethod
    def observation(self) -> CursorObservation: ...

    @abstractmethod
    def proves_input(self, input_id: InputId) -> bool: ...

    @abstractmethod
    def require_input_progress(self, proof: HistoricalNativeInput) -> None: ...

    @abstractmethod
    def claim_mismatch(self, injected_seq: int) -> bool: ...

    @abstractmethod
    def publish(self, db: Connection, owner: CursorOwner, coverage: ProvenSourceCoverage, proof: CoverageInput) -> CursorState: ...

    @abstractmethod
    def require_coverage(self, owner: CursorOwner, db: Connection, coverage: ProvenSourceCoverage, proof: CoverageInput) -> None: ...


@dataclass(frozen=True)
class UnstartedCursor(CursorState):
    @property
    def covered_seq(self) -> int:
        return 0

    @property
    def cursor(self) -> CurrentNativeCursor | None:
        return None

    @property
    def injected_seq(self) -> int:
        return 0  # No injected native input; cursor presence is this state's type.

    def proves_input(self, input_id: InputId) -> bool:
        return False

    def require_input_progress(self, proof: HistoricalNativeInput) -> None:
        """No preceding input constrains the first verified input."""

    def claim_mismatch(self, injected_seq: int) -> bool:
        return False

    def without_input(self, proof: NoInjectedInput, owner: CursorOwner, coverage: ProvenSourceCoverage) -> CurrentNativeCursor:
        return proof.build(owner, coverage.covered_seq)

    def observation(self) -> CursorObservation:
        return EmptyCursorObservation()

    def publish(self, db: Connection, owner: CursorOwner, coverage: ProvenSourceCoverage, proof: CoverageInput) -> CursorState:
        if coverage.covered_seq <= self.covered_seq:
            return self  # A blocked first source has not established a cursor.
        cursor = proof.candidate(self, owner, coverage)
        cursor.insert(db)
        return RecordedCursor(cursor)

    def require_coverage(self, owner: CursorOwner, db: Connection, coverage: ProvenSourceCoverage, proof: CoverageInput) -> None:
        pass  # No durable cursor exists to assert coverage.


@dataclass(frozen=True)
class RecordedCursor(CursorState):
    row: CurrentNativeCursor

    @property
    def covered_seq(self) -> int:
        return self.row.covered_seq

    @property
    def cursor(self) -> CurrentNativeCursor | None:
        return self.row

    @property
    def injected_seq(self) -> int:
        return self.row.injected_seq

    def without_input(self, proof: NoInjectedInput, owner: CursorOwner, coverage: ProvenSourceCoverage) -> CurrentNativeCursor:
        return replace(self.row, owner_claimed_generation=owner.claimed_generation, covered_seq=coverage.covered_seq)

    def observation(self) -> CursorObservation:
        return VerifiedCursorObservation(self.row)

    def proves_input(self, input_id: InputId) -> bool:
        return self.row.input_id.holds(input_id)

    def require_progress(self, owner: CursorOwner, coverage: ProvenSourceCoverage, injected: int) -> None:
        super().require_progress(owner, coverage, injected)
        if (self.row.owner_identity != owner.participant_identity
                or injected < self.row.injected_seq):
            raise IdentityConflict("current cursor would change owner or regress")

    def require_input_progress(self, proof: HistoricalNativeInput) -> None:
        if proof.source_seq <= self.row.injected_seq and not self.proves_input(proof.input_id):
            raise IdentityConflict("current cursor native input changed")

    def claim_mismatch(self, injected_seq: int) -> bool:
        if injected_seq > self.row.injected_seq:
            return False
        raise IdentityConflict("current cursor input claim_scope differs")

    def publish(self, db: Connection, owner: CursorOwner, coverage: ProvenSourceCoverage, proof: CoverageInput) -> CursorState:
        cursor = proof.candidate(self, owner, coverage)
        if cursor.covered_seq == self.row.covered_seq and cursor.injected_seq == self.row.injected_seq:
            return self
        return self.write(db, owner, cursor)

    def write(self, db: Connection, owner: CursorOwner, cursor: CurrentNativeCursor) -> CursorState:
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

    def require_coverage(self, owner: CursorOwner, db: Connection, coverage: ProvenSourceCoverage, proof: CoverageInput) -> None:
        owner.require_coverage(db, self.row, coverage)
        proof.require_reference(owner, self.row)


@dataclass(frozen=True)
class InheritedCursor(RecordedCursor):
    """An earlier claim's row; this claim's first publish writes its own row from it."""

    def write(self, db: Connection, owner: CursorOwner, cursor: CurrentNativeCursor) -> CursorState:
        cursor.insert(db)
        return RecordedCursor(cursor)
