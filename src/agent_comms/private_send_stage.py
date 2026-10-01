"""Selected stage declarations own claim, reservation and prelaunch-binding identity."""

from __future__ import annotations

import secrets
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .assignment_states import DeferredAssignment
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_errors import IdentityConflict, StaleFence
from .coordination_tables.assignments import WakeAssignment
from .coordinator import Coordination
from .durable_turn import DurableTurn
from .native_admission_rules import (
    NativeBindingCheck,
    NativeClaimCheck,
    NativeIdentityCheck,
    NativeReservationCheck,
)
from .native_input_owner import ParticipantOwner
from .native_input_record import (NativeInputIdentity, NativeInputExecution, TriageNativeExecution, FullNativeExecution)
from .native_pi import NativeContextProof
from .native_prompt_binding import (
    PromptBinding,
    expected_prompt_matches_journal,
    read_expected_prompt_binding,
)
from .native_runtime_input import NativeRuntimeInput
from .private_sidecar import native_request_digest
from .reservation_rules import ReservationViolationError
from .selected_triage import SelectedTriage
from .text_digest import TextDigest


@dataclass(frozen=True)
class NativeSendStage(ABC):
    assignment: WakeAssignment

    @property
    @abstractmethod
    def execution(self) -> NativeInputExecution: ...

    @abstractmethod
    def require_phase(self, store: Coordination, current: WakeAssignment) -> None: ...

    @abstractmethod
    def reserve_claim(self, store: Coordination, db: sqlite3.Connection) -> None:
        """Fence this stage and reject any previous dispatch before inserting an ID."""

    @abstractmethod
    def fail_terminal(self) -> None:
        """Settle only the stage's proved, reaped failure; never grant replay."""

    def fail_unknown(self, bus, owner, witness) -> None:
        owner.require_registry(bus._registry)

    def reserve(self, store: Coordination, owner: ParticipantOwner, token_digest: str) -> str:
        input_id = secrets.token_hex(16)
        with store.session.transaction() as db:
            assert_native_runtime_schema(db)
            owner.require(store, self.assignment.recipient_lookup)
            self.reserve_claim(store, db)
            NativeRuntimeInput(
                input_id=input_id,
                execution=self.execution,
                assignment_id=self.assignment.assignment_id,
                owner_lookup=self.assignment.recipient_lookup,
                owner_thread=owner.thread.name,
                owner_generation=owner.generation,
                owner_token_digest=token_digest,
            ).insert(db)
        return input_id

    def identity(self, input_id: str, owner) -> NativeInputIdentity:
        return NativeInputIdentity(
            input_id, self.assignment.assignment_id, self.execution, owner,
        )

    def pending_input(
        self, store: Coordination, input_id: str, owner: ParticipantOwner, token_digest: str
    ) -> NativeRuntimeInput:
        """Read current durable ownership inside the caller's read/write transaction."""
        db = store.session._connection
        assert_native_runtime_schema(db)
        owner.require(store, self.assignment.recipient_lookup)
        row = NativeRuntimeInput.one(db, input_id=input_id)
        if row is None:
            raise StaleFence("native input reservation is missing")
        try:
            NativeIdentityCheck(
                row=row,
                stage=self,
                owner=owner.coordinator_identity(self.assignment.recipient_lookup),
            ).require_valid()
        except ReservationViolationError as error:
            raise StaleFence(f"native dispatch identity changed: {error}") from error
        row.require_unproven(token_digest)
        return row

    def verify(
        self,
        store: Coordination,
        owner: ParticipantOwner,
        input_id: str,
        token_digest: str,
        context: NativeContextProof,
        *,
        session_dir: Path,
        wire_root_id: str,
        prompt: str,
    ) -> None:
        # The native boundary validated the live RPC events before returning.
        # Disk evidence corroborates those events, never authorizes recovery.
        if not context.corroborates_input(input_id, session_dir):
            raise IdentityConflict(
                "Pi live assembled context differs from its reserved input proof"
            )
        with store.session.read():
            self.pending_input(store, input_id, owner, token_digest)
            binding = self.require_binding(
                store,
                input_id,
                owner,
                wire_root_id,
                prompt,
                blocking=True,
            )
            if not expected_prompt_matches_journal(context.session_file, binding):
                raise IdentityConflict("live native input lacks exact bound source prompt equality")

    def require_claim(self, store: Coordination) -> None:
        current = store.assignments.get(self.assignment.assignment_id)
        try:
            NativeClaimCheck(captured=self.assignment, current=current).require_valid()
        except ReservationViolationError as error:
            raise StaleFence(
                f"selected claim identity changed before native send: {error}"
            ) from error
        self.require_phase(store, current)

    def require_reservation(
        self, db: sqlite3.Connection, input_id: str, owner: ParticipantOwner, token_digest: str
    ) -> NativeRuntimeInput:
        reserved = NativeRuntimeInput.one(db, input_id=input_id)
        if reserved is None:
            raise StaleFence("native reservation missing before send")
        try:
            NativeReservationCheck(
                row=reserved,
                stage=self,
                owner=owner.coordinator_identity(self.assignment.recipient_lookup),
                token_digest=token_digest,
            ).require_valid()
        except ReservationViolationError as error:
            raise StaleFence(f"native reservation changed before send: {error}") from error
        return reserved

    def require_binding(
        self,
        store: Coordination,
        input_id: str,
        owner: ParticipantOwner,
        wire_root_id: str,
        prompt: str,
        *,
        blocking: bool = False,
    ) -> PromptBinding:
        binding = read_expected_prompt_binding(store, input_id, blocking=blocking)
        if binding is None:
            raise IdentityConflict("native send has no durable prompt binding")
        try:
            NativeBindingCheck(
                row=binding,
                stage=self,
                owner=owner.coordinator_identity(self.assignment.recipient_lookup),
                wire_root_id=wire_root_id,
                prompt_digest=TextDigest(native_request_digest(prompt)),
            ).require_valid()
        except ReservationViolationError as error:
            raise IdentityConflict(
                f"native send differs from its durable prompt binding: {error}"
            ) from error
        return binding


class TriageNativeSend(NativeSendStage):
    @property
    def execution(self) -> TriageNativeExecution:
        return TriageNativeExecution()

    def fail_terminal(self) -> None:
        # Reservation already deferred the triage assignment. A terminal failure
        # has no execution attempt to settle and must not create a fresh triage.
        return None

    def reserve_claim(self, store: Coordination, db: sqlite3.Connection) -> None:
        current = store.assignments.get(self.assignment.assignment_id)
        if current != self.assignment or not current.lifecycle.triage_pending:
            raise IdentityConflict("triage claim changed before native input reservation")
        if NativeRuntimeInput.select(
            db,
            where="assignment_id=?",
            parameters=(self.assignment.assignment_id,),
        ):
            raise IdentityConflict("triage input was previously dispatched; no retry")
        # This CAS and the reserved input ID commit BEFORE launching Pi.
        update = WakeAssignment.update(
            db,
            where="assignment_id=? AND revision=? AND disposition='triage_pending'",
            parameters=(current.assignment_id, current.revision),
            lifecycle=DeferredAssignment.build(current.lifecycle.mode, None, None),
            revision=current.revision + 1,
            updated_at_ms=store.session.now(current.updated_at_ms),
        )
        if update.rowcount != 1:
            raise IdentityConflict("triage reservation lost its claim CAS")

    def commit(
        self,
        store: Coordination,
        owner: ParticipantOwner,
        input_id: str,
        token_digest: str,
        context: NativeContextProof,
        decision: SelectedTriage,
    ) -> None:
        with store.session.transaction() as db:
            row = self.pending_input(store, input_id, owner, token_digest)
            current = store.assignments.get(self.assignment.assignment_id)
            self.require_phase(store, current)
            row.commit_context(db, context, verdict=type(decision))
            decision.settle(store, db, current)

    def require_phase(self, store: Coordination, current: WakeAssignment) -> None:
        if (
            current.lifecycle
            != DeferredAssignment.build(self.assignment.lifecycle.mode, None, None)
            or current.revision != self.assignment.revision + 1
        ):
            raise StaleFence("triage claim changed before native send")


@dataclass(frozen=True)
class FullNativeSend(NativeSendStage):
    progress: DurableTurn

    @property
    def fence(self):
        # Native observations advance the existing durable attempt owner. Never
        # capture a second fence which goes stale while tools/model events run.
        return self.progress.fence

    @property
    def execution(self) -> FullNativeExecution:
        return FullNativeExecution(self.fence.execution_id, self.fence.attempt_ordinal)

    def fail_terminal(self) -> None:
        self.progress.fail_terminal()

    def fail_unknown(self, bus, owner, witness) -> None:
        from .coordination_response import _response_boundary

        with _response_boundary(bus) as registry:
            witness.require_live(registry, self.fence)
            self.progress.fail_unknown()

    def reserve_claim(self, store: Coordination, db: sqlite3.Connection) -> None:
        snapshot, _ = store.attempts.require_fence(self.fence)
        self.require_claim(store)
        if snapshot.pointer_revision < 1 or self.assignment not in snapshot.assignments:
            raise StaleFence("full input cannot bind to the current attempt")
        if NativeRuntimeInput.select(
            db,
            where="execution_id=?",
            parameters=(self.execution.execution_id,),
        ):
            raise IdentityConflict("a full-turn input already exists; no automatic replay")

    def commit(
        self,
        store: Coordination,
        owner: ParticipantOwner,
        input_id: str,
        token_digest: str,
        context: NativeContextProof,
    ) -> None:
        with store.session.transaction() as db:
            row = self.pending_input(store, input_id, owner, token_digest)
            snapshot, _ = store.attempts.require_fence(self.fence)
            engagement = self.assignment.lifecycle.require_engagement()
            if snapshot.execution.exact_target != engagement.exact_target:
                raise StaleFence("full-turn proof lost its exact reply target")
            row.commit_context(db, context)

    def require_phase(self, store: Coordination, current: WakeAssignment) -> None:
        snapshot, attempt = store.attempts.require_fence(self.fence)
        if (
            not current.lifecycle.engaged
            or not snapshot.execution.lifecycle.active
            or not attempt.lifecycle.starting
            or attempt.lifecycle.backend_done
            or attempt.lifecycle.process_dead
        ):
            raise StaleFence("full execution is not running before native send")
