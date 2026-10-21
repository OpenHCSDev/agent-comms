"""Current-owner source cursor; captured state owns incremental proof and publication."""
from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_database import CoordinationStore
from .coordination_errors import IdentityConflict, StaleFence
from .coordinator import Coordination
from .cursor_owner import CursorOwner
from .field_codec import FieldCodec
from .message_bus import MessageBus
from .native_input_owner import RegistryOwner
from .native_runtime_input import CurrentNativeCursor
from .proven_source_coverage import SourceCoverage
from .store_files import StoreLockContention
from .threads import Thread

if TYPE_CHECKING:
    from .native_entries import NativeEvidenceScope


class NativeSourceCursor:
    def __init__(self, bus: MessageBus, store: Coordination, *, wire_root_id: str) -> None:
        self.bus, self.store, self.wire_root_id = bus, store, wire_root_id

    @classmethod
    async def read_async(cls, bus, *, wire_root_id, owner_name, source_reads):
        return await Coordination.run_read_async(
            CoordinationStore.path_in(bus.log.path.parent),
            partial(cls._read_owned, bus, wire_root_id, owner_name, source_reads),
        )

    @classmethod
    def _read_owned(cls, bus, wire_root_id, owner_name, source_reads, store):
        return cls(bus, store, wire_root_id=wire_root_id).read(owner_name=owner_name, source_reads=source_reads)

    @classmethod
    async def advance_async(cls, bus, *, wire_root_id, owner, owner_claimed_generation,
                            owner_generation, committed_input_id, source_reads):
        return await Coordination.run_async(
            CoordinationStore.path_in(bus.log.path.parent),
            partial(cls._advance_owned, bus, wire_root_id, owner, owner_claimed_generation,
                    owner_generation, committed_input_id, source_reads=source_reads),
        )

    @classmethod
    def _advance_owned(cls, bus, wire_root_id, owner, claim_scope, generation, input_id, store, *, source_reads):
        return cls(bus, store, wire_root_id=wire_root_id).advance(
            owner=owner, owner_claimed_generation=claim_scope, owner_generation=generation,
            committed_input_id=input_id, source_reads=source_reads,
        )

    @classmethod
    async def refresh_async(cls, bus, *, wire_root_id, owner_name, source_reads):
        return await Coordination.run_async(
            CoordinationStore.path_in(bus.log.path.parent),
            partial(cls._refresh_owned, bus, wire_root_id, owner_name, source_reads),
        )

    @classmethod
    def _refresh_owned(cls, bus, wire_root_id, owner_name, source_reads, store):
        owner, claim_scope = bus._registry.live_owner_with_claim(owner_name)
        person = store.participants.get(owner.incarnation.lookup)
        return cls._advance_owned(bus, wire_root_id, owner, claim_scope, person.participant_generation,
                                  None, store, source_reads=source_reads)

    def _coverage(self, lookup, contention=None):
        return SourceCoverage(self.bus, self.store, wire_root_id=self.wire_root_id,
                              recipient_lookup=lookup, contention=contention)

    def advance(self, *, owner: Thread, owner_claimed_generation: int, owner_generation: int,
                committed_input_id: str | None, source_reads: NativeEvidenceScope) -> CurrentNativeCursor | None:
        committed_input_id = FieldCodec.decode(str | None, committed_input_id)
        identity = CursorOwner(wire_root_id=self.wire_root_id, thread=owner,
                               generation=owner_generation, claimed_generation=owner_claimed_generation)
        with source_reads.borrow():
            return self._advance_proven(identity, committed_input_id, source_reads, StoreLockContention(2.0))

    def _advance_proven(self, identity, committed_input_id, source_reads, contention):
        sources = self._coverage(identity.lookup, contention)
        with self.store.session.boundary(self.bus, blocking=False, contention=contention):
            with self.store.session.read():
                state = identity.state(self.store.session._connection,
                                       self.bus.log.private_marker_unlocked().claimed_after_seq)
        coverage = sources.since(state, source_reads=source_reads)
        proof = coverage.proof
        with (self.store.session.boundary(self.bus, blocking=False, contention=contention) as boundary,
              self.store.session.writing() as db):
            if sources.witness_unlocked() != coverage.source_witness:
                raise IdentityConflict('current cursor canonical source changed before commit')
            identity.require_live(self.bus, boundary.registry, 'current cursor owner or private root changed')
            identity.require_participant(self.store, 'current cursor recipient generation changed')
            if identity.cursor(db) != state.cursor:
                raise StaleFence('current cursor SQL row changed during source verification')
            injected = proof.injected_for(state)
            state.require_progress(identity, coverage, injected)
            if identity.matches_prefix(db, coverage.evidence()) and proof.allows(
                identity, db, state, committed_input_id,
            ):
                state = state.publish(db, identity, coverage, proof)
            state.require_coverage(identity, db, coverage, proof)
            return state.cursor

    def read(self, *, owner_name, source_reads):
        with self.store.session.boundary(self.bus, blocking=False) as boundary:
            reason = 'current native cursor has no matching live owner'
            marker = self.bus.log.private_marker_unlocked()
            if marker.root_id != self.wire_root_id:
                raise StaleFence(reason)
            captured = RegistryOwner.capture(boundary.registry, owner_name, reason)
            with self.store.session.read():
                db = self.store.session._connection
                assert_native_runtime_schema(db)
                person = self.store.participants.get(captured.thread.incarnation.lookup)
                if not person.committed or person.owner_thread != owner_name:
                    raise StaleFence('current native cursor recipient is not committed')
                identity = CursorOwner(wire_root_id=self.wire_root_id, thread=captured.thread,
                                       generation=person.participant_generation,
                                       claimed_generation=captured.claimed_generation)
                state = identity.state(db, marker.claimed_after_seq)
        sources = self._coverage(identity.lookup)
        with source_reads.borrow():
            witness = state.source_witness(identity, sources, source_reads)
        with self.store.session.boundary(self.bus, blocking=False) as boundary:
            if sources.witness_unlocked() != witness:
                raise IdentityConflict('current cursor canonical source changed while reading')
            identity.require_live(self.bus, boundary.registry, 'current native cursor owner changed while reading')
            with self.store.session.read():
                identity.require_participant(self.store, 'current native cursor participant generation changed')
                if identity.cursor(self.store.session._connection) != state.cursor:
                    raise StaleFence('current native cursor SQL row changed while reading')
        return state.cursor
