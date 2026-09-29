"""Selected stage declarations own claim, reservation and prelaunch-binding identity."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .coordination_errors import IdentityConflict, StaleFence
from .coordination_tables.assignments import WakeAssignment
from .coordinator import Coordination
from .native_admission_rules import NativeBindingCheck, NativeClaimCheck, NativeReservationCheck
from .native_input_owner import ParticipantOwner
from .native_input_record import NativeInputRecord
from .native_prompt_binding import read_expected_prompt_binding
from .native_runtime_input import NativeRuntimeInput
from .owner_fence import OwnerFence
from .private_sidecar import native_request_digest
from .reservation_rules import ReservationViolationError
from .text_digest import TextDigest


@dataclass(frozen=True)
class NativeSendStage(ABC):
    assignment: WakeAssignment

    @property
    @abstractmethod
    def stage(self) -> str: ...

    @property
    @abstractmethod
    def execution_id(self) -> str | None: ...

    @property
    @abstractmethod
    def attempt_ordinal(self) -> int | None: ...

    @abstractmethod
    def require_phase(self, store: Coordination, current: WakeAssignment) -> None: ...

    @abstractmethod
    def matches_attempt(self, row: NativeInputRecord) -> bool: ...

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
    ) -> None:
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

    def require_binding(
        self,
        store: Coordination,
        input_id: str,
        owner: ParticipantOwner,
        wire_root_id: str,
        prompt: str,
    ) -> None:
        binding = read_expected_prompt_binding(store, input_id, blocking=False)
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


class TriageNativeSend(NativeSendStage):
    stage: ClassVar[str] = "triage"
    execution_id: ClassVar[None] = None
    attempt_ordinal: ClassVar[None] = None

    def matches_attempt(self, row: NativeInputRecord) -> bool:
        return row.execution_id is None and row.attempt_ordinal is None

    def require_phase(self, store: Coordination, current: WakeAssignment) -> None:
        if not current.lifecycle.deferred or current.revision != self.assignment.revision + 1:
            raise StaleFence("triage claim changed before native send")


@dataclass(frozen=True)
class FullNativeSend(NativeSendStage):
    fence: OwnerFence
    stage: ClassVar[str] = "full"

    @property
    def execution_id(self) -> str:
        return self.fence.execution_id

    @property
    def attempt_ordinal(self) -> int:
        return self.fence.attempt_ordinal

    def matches_attempt(self, row: NativeInputRecord) -> bool:
        return (
            row.execution_id == self.fence.execution_id
            and row.attempt_ordinal == self.fence.attempt_ordinal
        )

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
