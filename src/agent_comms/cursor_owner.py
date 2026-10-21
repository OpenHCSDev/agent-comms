"""Captured owner and typed initial/recorded states for native source cursors."""
from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_errors import IdentityConflict, StaleFence
from .coordination_database import CoordinationStore
from .field_codec import FieldCodec
from .coordinator import Coordination
from .message_bus import MessageBus
from .native_input_owner import RegistryOwner, ParticipantOwner
from .native_input_record import NativeInputReference
from .thread_identity import GenerationCounter
from .native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from .registry_document import RegistrySnapshot
from .private_send_rules import RunningTurnRegistryCheck

if TYPE_CHECKING:
    from .acp_extension import CursorObservation
    from .native_entries import NativeEvidenceScope


@dataclass(frozen=True)
class CursorOwner(RegistryOwner):
    """Record this started input; deciding whether tools may run belongs to its turn."""

    check_type = RunningTurnRegistryCheck
    wire_root_id: str
    generation: int

    def __post_init__(self):
        GenerationCounter.require_positive(self.generation)
        GenerationCounter.require_positive(self.claimed_generation)

    @classmethod
    async def read_async(cls, bus: MessageBus, *, wire_root_id: str, owner_name: str,
                         source_reads: NativeEvidenceScope) -> CursorState:
        return await Coordination.run_read_async(
            CoordinationStore.path_in(bus.bus.path.parent),
            partial(cls._read_owned, bus, wire_root_id, owner_name, source_reads),
        )

    @classmethod
    def _read_owned(cls, bus, root_id, owner_name, source_reads, store) -> CursorState:
        from .proven_source_coverage import SourcePrefix

        with store.session.boundary(bus) as boundary:
            reason = "Current native cursor has no matching live owner"
            bus._registry.store.private_guard_unlocked().require_root(root_id, StaleFence(reason))
            captured = RegistryOwner.capture(boundary.registry, owner_name, reason)
            with store.session.read():
                db = store.session._connection
                assert_native_runtime_schema(db)
                person = store.participants.get(captured.thread.incarnation.lookup)
                if not person.committed or person.owner_thread != owner_name:
                    raise StaleFence("Current cursor recipient is not committed")
                owner = cls(thread=captured.thread, claimed_generation=captured.claimed_generation,
                            wire_root_id=root_id, generation=person.participant_generation)
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

    @classmethod
    async def advance_async(cls, bus: MessageBus, *, wire_root_id: str, owner,
                            owner_claimed_generation: int, owner_generation: int,
                            committed_input_id: str | None,
                            source_reads: NativeEvidenceScope) -> CursorState:
        captured = cls(thread=owner, claimed_generation=owner_claimed_generation,
                       wire_root_id=wire_root_id, generation=owner_generation)
        return await Coordination.run_async(
            CoordinationStore.path_in(bus.bus.path.parent),
            partial(captured._advance_owned, bus, committed_input_id, source_reads),
        )

    @classmethod
    async def refresh_async(cls, bus: MessageBus, *, wire_root_id: str, owner_name: str,
                            source_reads: NativeEvidenceScope) -> CursorState:
        return await Coordination.run_async(
            CoordinationStore.path_in(bus.bus.path.parent),
            partial(cls._refresh_owned, bus, wire_root_id, owner_name, source_reads),
        )

    @classmethod
    def _refresh_owned(cls, bus, root_id, owner_name, source_reads, store) -> CursorState:
        thread, generation = bus._registry.live_owner_with_claim(owner_name)
        person = store.participants.get(thread.incarnation.lookup)
        owner = cls(thread=thread, claimed_generation=generation, wire_root_id=root_id,
                    generation=person.participant_generation)
        return owner._advance_owned(bus, None, source_reads, store)

    def _advance_owned(self, bus, committed_input_id, source_reads, store) -> CursorState:
        from .proven_source_coverage import SourceCoverage

        committed_input_id = FieldCodec.decode(str | None, committed_input_id)
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
                    self, db, state, committed_input_id,
                ):
                    state = state.publish(db, self, coverage, proof)
                state.require_coverage(self, db, coverage, proof)
                return state

    @property
    def participant_identity(self):
        return ParticipantOwner(self.thread, self.generation).coordinator_identity(self.lookup)

    @property
    def lookup(self) -> str:
        return self.thread.incarnation.lookup

    def require_live(self, bus: MessageBus, registry: RegistrySnapshot, reason: str) -> None:
        bus._registry.store.private_guard_unlocked().require_root(self.wire_root_id, StaleFence(reason))
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
