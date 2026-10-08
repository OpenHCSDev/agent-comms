"""Current-owner private source cursor; never work, ACK, retry or write authority.

Historical journal observations are collected outside the write transaction.
Publication rechecks their immutable SQL identities and the canonical bus witness
under wire→bus→registry→SQL locks. Reads recheck both owner and SQL after the scan.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING
from functools import partial

from .bus_publication import stable_thread_lookup
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_errors import IdentityConflict, StaleFence
from .coordination_response import _response_boundary
from .coordinator import Coordination
from .cursor_owner import CursorOwner
from .historical_native_inputs import HistoricalNativeInput
from .message_bus import MessageBus
from .native_input_owner import RegistryOwner
from .native_runtime_input import CurrentNativeCursor
from .proven_source_coverage import ProvenSourceCoverage, SourceCoverage
from .threads import Thread
from .field_codec import FieldCodec
from .store_files import StoreLockContention

if TYPE_CHECKING:
    from .native_entries import NativeEvidenceScope


class NativeSourceCursor:
    """Own current cursor observation and monotonic publication for one private bus."""

    def __init__(self, bus: MessageBus, store: Coordination, *, wire_root_id: str) -> None:
        if type(bus) is not MessageBus or type(store) is not Coordination:
            raise ValueError("current native cursor needs actual private stores")
        self.bus, self.store, self.wire_root_id = bus, store, wire_root_id

    @classmethod
    async def read_async(cls, bus: MessageBus, *, wire_root_id: str, owner_name: str):
        return await Coordination.run_async(
            bus.log.path.parent / "coordination.sqlite3",
            partial(cls._read_owned, bus, wire_root_id, owner_name),
        )

    @classmethod
    def _read_owned(cls, bus, wire_root_id, owner_name, store):
        return cls(bus, store, wire_root_id=wire_root_id).read(owner_name=owner_name)

    @classmethod
    async def advance_async(
        cls, bus: MessageBus, *, wire_root_id: str, owner: Thread,
        owner_admission_generation: int, owner_generation: int,
        committed_input_id: str | None,
    ):
        return await Coordination.run_async(
            bus.log.path.parent / "coordination.sqlite3",
            partial(cls._advance_owned, bus, wire_root_id, owner,
                    owner_admission_generation, owner_generation, committed_input_id),
        )

    @classmethod
    def _advance_owned(cls, bus, wire_root_id, owner, admission, generation, input_id, store):
        return cls(bus, store, wire_root_id=wire_root_id).advance(
            owner=owner, owner_admission_generation=admission,
            owner_generation=generation, committed_input_id=input_id,
        )

    @classmethod
    async def refresh_async(cls, bus: MessageBus, *, wire_root_id: str, owner_name: str):
        return await Coordination.run_async(
            bus.log.path.parent / "coordination.sqlite3",
            partial(cls._refresh_owned, bus, wire_root_id, owner_name),
        )

    @classmethod
    def _refresh_owned(cls, bus, wire_root_id, owner_name, store):
        owner, admission = bus._registry.live_owner_with_admission(owner_name)
        person = store.participants.get(stable_thread_lookup(owner.created_at))
        return cls._advance_owned(
            bus, wire_root_id, owner, admission, person.participant_generation, None, store
        )

    def _coverage(self, lookup: str, contention: StoreLockContention | None = None) -> SourceCoverage:
        return SourceCoverage(
            self.bus, self.store, wire_root_id=self.wire_root_id, recipient_lookup=lookup,
            contention=contention
        )

    def advance(
        self,
        *,
        owner: Thread,
        owner_admission_generation: int,
        owner_generation: int,
        committed_input_id: str | None,
    ) -> CurrentNativeCursor | None:
        from .native_entries import NativeEvidenceScope

        committed_input_id = FieldCodec.decode(str | None, committed_input_id)
        identity = CursorOwner(
            wire_root_id=self.wire_root_id,
            thread=owner,
            generation=owner_generation,
            admission_generation=owner_admission_generation,
        )
        with NativeEvidenceScope() as source_reads:
            return self._advance_proven(identity, committed_input_id, source_reads,
                StoreLockContention(2.0) if committed_input_id is not None else None)

    def _advance_proven(
        self, identity: CursorOwner, committed_input_id: str | None,
        source_reads: NativeEvidenceScope,
        contention: StoreLockContention | None,
    ) -> CurrentNativeCursor | None:
        sources = self._coverage(identity.lookup, contention)
        coverage = sources.prefix(source_reads=source_reads)
        proof = coverage.last_proof(
            coverage.injected_source_seqs[-1] if coverage.injected_source_seqs else 0,
        )
        evidence = coverage.evidence()
        with (
            _response_boundary(self.bus, blocking=False, contention=contention) as registry,
            self.store.session.transaction() as db,
        ):
            if sources.witness_unlocked() != coverage.source_witness:
                raise IdentityConflict("current cursor canonical source changed before commit")
            identity.require_live(
                self.bus, registry, "current cursor owner or private root changed"
            )
            identity.require_participant(self.store, "current cursor recipient generation changed")
            prior = identity.cursor(db)
            injected = coverage.injected_source_seqs[-1] if coverage.injected_source_seqs else 0
            if prior is None and coverage.covered_seq == 0:
                return None  # A blocked first source is not a zero-valued cursor.
            if prior is not None and (
                prior.owner_identity != identity.participant_identity
                or coverage.covered_seq < prior.covered_seq
                or injected < prior.injected_seq
            ):
                raise IdentityConflict("current cursor would change owner or regress")
            cursor = prior
            if identity.matches_prefix(db, evidence) and identity.admits(
                db, proof, injected, prior, committed_input_id
            ):
                cursor = self._publish(db, identity, prior, coverage, proof)
            if cursor is not None:
                identity.require_coverage(db, cursor, coverage)
            return cursor

    @staticmethod
    def _publish(
        db: sqlite3.Connection,
        owner: CursorOwner,
        prior: CurrentNativeCursor | None,
        coverage: ProvenSourceCoverage,
        proof: HistoricalNativeInput | None,
    ) -> CurrentNativeCursor:
        cursor = CurrentNativeCursor(
            wire_root_id=owner.wire_root_id,
            recipient_lookup=owner.lookup,
            owner_thread=owner.thread.name,
            owner_generation=owner.generation,
            owner_admission_generation=owner.admission_generation,
            covered_seq=coverage.covered_seq,
            injected_seq=coverage.injected_source_seqs[-1] if coverage.injected_source_seqs else 0,
            input_id=proof.input_id if proof else None,
            stage=type(proof.execution) if proof else None,
            session_id=proof.context.session_id if proof else None,
            request_generation=proof.context.request_generation if proof else None,
        )
        if prior is None:
            cursor.insert(db)
        elif cursor.covered_seq > prior.covered_seq or cursor.injected_seq > prior.injected_seq:
            updated = CurrentNativeCursor.update(
                db,
                where="wire_root_id=? AND recipient_lookup=? AND owner_generation=? "
                "AND owner_admission_generation=? AND covered_seq=? AND injected_seq=?",
                parameters=(
                    owner.wire_root_id,
                    owner.lookup,
                    owner.generation,
                    owner.admission_generation,
                    prior.covered_seq,
                    prior.injected_seq,
                ),
                covered_seq=cursor.covered_seq,
                injected_seq=cursor.injected_seq,
                input_id=cursor.input_id,
                stage=cursor.stage,
                session_id=cursor.session_id,
                request_generation=cursor.request_generation,
            )
            if updated.rowcount != 1:
                raise StaleFence("current cursor monotonic update lost its fence")
        else:
            # Equal bounds did not write a new row. Publish the exact durable
            # result, including its original native reference, rather than a
            # newly constructed candidate.
            return prior
        return cursor

    def read(self, *, owner_name: str) -> CurrentNativeCursor | None:
        with _response_boundary(self.bus, blocking=False) as registry:
            reason = "current native cursor has no matching live owner"
            if self.bus.log._private_marker_unlocked().root_id != self.wire_root_id:
                raise StaleFence(reason)
            captured = RegistryOwner.capture(registry, owner_name, reason)
            with self.store.session.read():
                db = self.store.session._connection
                assert_native_runtime_schema(db)
                person = self.store.participants.get(
                    stable_thread_lookup(captured.thread.created_at)
                )
                if not person.committed or person.owner_thread != owner_name:
                    raise StaleFence("current native cursor recipient is not committed")
                identity = CursorOwner(
                    wire_root_id=self.wire_root_id,
                    thread=captured.thread,
                    generation=person.participant_generation,
                    admission_generation=captured.admission_generation,
                )
                cursor = identity.cursor(db)
                if cursor is not None:
                    identity.require_recorded_input(db, cursor)
        sources = self._coverage(identity.lookup)
        witness = None
        if cursor is not None:
            from .native_entries import NativeEvidenceScope

            with NativeEvidenceScope() as source_reads:
                coverage = self._require_source(identity, cursor, sources, source_reads)
                witness = coverage.source_witness
        with _response_boundary(self.bus, blocking=False) as registry:
            if cursor is not None and sources.witness_unlocked() != witness:
                raise IdentityConflict("current cursor canonical source changed while reading")
            identity.require_live(
                self.bus, registry, "current native cursor owner changed while reading"
            )
            with self.store.session.read():
                identity.require_participant(
                    self.store, "current native cursor participant generation changed"
                )
                if identity.cursor(self.store.session._connection) != cursor:
                    raise StaleFence("current native cursor SQL row changed while reading")
        return cursor

    def _require_source(
        self, owner: CursorOwner, cursor: CurrentNativeCursor, sources: SourceCoverage,
        source_reads: NativeEvidenceScope,
    ) -> ProvenSourceCoverage:
        coverage = sources.prefix(through_seq=cursor.covered_seq, source_reads=source_reads)
        with self.store.session.read():
            assert_native_runtime_schema(self.store.session._connection)
            owner.require_coverage(self.store.session._connection, cursor, coverage)
        return coverage
