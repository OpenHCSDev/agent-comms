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
from .native_input_owner import RegistryOwner
from .native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from .registry_document import RegistrySnapshot


@dataclass(frozen=True)
class CursorOwner(RegistryOwner):
    """A captured identity, never an admission permit until rechecked under locks."""

    wire_root_id: str
    generation: int

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
        if (
            not person.committed
            or person.owner_thread != self.thread.name
            or person.participant_generation != self.generation
        ):
            raise StaleFence(reason)

    def cursor(self, db: sqlite3.Connection) -> CurrentNativeCursor | None:
        return CurrentNativeCursor.one(
            db,
            wire_root_id=self.wire_root_id,
            recipient_lookup=self.lookup,
            owner_generation=self.generation,
            owner_admission_generation=self.admission_generation,
        )

    def _matches_input(
        self,
        row: NativeRuntimeInput | None,
        *,
        assignment_id: str,
        stage: str,
        session_id: str,
        request_generation: int,
    ) -> bool:
        return row is not None and (
            row.owner_lookup,
            row.owner_thread,
            row.owner_generation,
            row.sent_owner_admission_generation,
            row.assignment_id,
            row.stage,
            row.session_id,
            row.request_generation,
        ) == (
            self.lookup,
            self.thread.name,
            self.generation,
            self.admission_generation,
            assignment_id,
            stage,
            session_id,
            request_generation,
        )

    def matches_prefix(
        self, db: sqlite3.Connection, evidence: tuple[HistoricalNativeInput, ...]
    ) -> bool:
        return all(
            self._matches_input(
                NativeRuntimeInput.one(db, input_id=item.input_id),
                assignment_id=item.assignment_id,
                stage=item.stage,
                session_id=item.context.session_id,
                request_generation=item.context.request_generation,
            )
            for item in evidence
        )

    def require_recorded_input(self, db: sqlite3.Connection, cursor: CurrentNativeCursor) -> None:
        if cursor.input_id is None:
            return
        row = NativeRuntimeInput.one(db, input_id=cursor.input_id)
        if row is None or row.sent_owner_admission_generation != self.admission_generation:
            raise IdentityConflict("current cursor input admission differs")
        if not self._matches_input(
            row,
            assignment_id=cursor.assignment_id,
            stage=cursor.stage,
            session_id=cursor.session_id,
            request_generation=cursor.request_generation,
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
        """Historical evidence cannot seed another live admission generation."""
        if proof is None:
            return True  # Covered no-wake prefix may precede an UNKNOWN gap.
        if (proof.owner_lookup, proof.owner_thread, proof.owner_generation, proof.source_seq) != (
            self.lookup,
            self.thread.name,
            self.generation,
            injected_seq,
        ):
            raise IdentityConflict("current cursor native proof belongs to another owner")
        if prior is None or injected_seq > prior.injected_seq:
            if proof.input_id != committed_input_id:
                return False
        elif prior.input_id != proof.input_id:
            raise IdentityConflict("current cursor native input changed")
        elif committed_input_id is not None and proof.input_id != committed_input_id:
            return False
        reserved = NativeRuntimeInput.one(db, input_id=proof.input_id)
        if (
            reserved is not None
            and reserved.sent_owner_admission_generation != self.admission_generation
        ):
            if prior is None or injected_seq > prior.injected_seq:
                return False
            raise IdentityConflict("current cursor input admission differs")
        if not self._matches_input(
            reserved,
            assignment_id=proof.assignment_id,
            stage=proof.stage,
            session_id=proof.context.session_id,
            request_generation=proof.context.request_generation,
        ):
            raise IdentityConflict("current cursor differs from committed native receipt")
        return True
