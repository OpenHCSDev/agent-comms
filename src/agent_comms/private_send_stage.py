"""Selected stage declarations own claim, reservation and prelaunch-binding identity."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .coordination_errors import IdentityConflict, StaleFence
from .coordination_tables.assignments import WakeAssignment
from .coordinator import Coordination
from .native_input_owner import ParticipantOwner
from .native_prompt_binding import read_expected_prompt_binding
from .native_runtime_input import NativeRuntimeInput
from .owner_fence import OwnerFence
from .private_sidecar import native_request_digest


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

    def require_claim(self, store: Coordination) -> None:
        current = store.assignments.get(self.assignment.assignment_id)
        if (
            current.recipient_lookup,
            current.recipient,
            current.wire_seq,
            current.message_id,
            current.lifecycle.mode,
        ) != (
            self.assignment.recipient_lookup,
            self.assignment.recipient,
            self.assignment.wire_seq,
            self.assignment.message_id,
            self.assignment.lifecycle.mode,
        ):
            raise StaleFence("selected claim identity changed before native send")
        self.require_phase(store, current)

    def require_reservation(
        self, db: sqlite3.Connection, input_id: str, owner: ParticipantOwner, token_digest: str
    ) -> None:
        reserved = NativeRuntimeInput.one(db, input_id=input_id)
        if reserved is None or (
            reserved.stage,
            reserved.assignment_id,
            reserved.owner_lookup,
            reserved.owner_thread,
            reserved.owner_generation,
            reserved.execution_id,
            reserved.attempt_ordinal,
            reserved.owner_token_digest,
            reserved.sent_owner_admission_generation,
            reserved.session_id,
            reserved.verdict,
        ) != (
            self.stage,
            self.assignment.assignment_id,
            self.assignment.recipient_lookup,
            owner.thread.name,
            owner.generation,
            self.execution_id,
            self.attempt_ordinal,
            token_digest,
            None,
            None,
            None,
        ):
            raise StaleFence("native reservation changed before send")

    def require_binding(
        self,
        store: Coordination,
        input_id: str,
        owner: ParticipantOwner,
        wire_root_id: str,
        prompt: str,
    ) -> None:
        binding = read_expected_prompt_binding(store, input_id, blocking=False)
        if binding is None or (
            binding.stage,
            binding.assignment_id,
            binding.owner_lookup,
            binding.owner_thread,
            binding.owner_generation,
            binding.wire_root_id,
            binding.source_seq,
            binding.message_id,
            binding.expected_prompt_digest,
            binding.execution_id,
            binding.attempt_ordinal,
        ) != (
            self.stage,
            self.assignment.assignment_id,
            self.assignment.recipient_lookup,
            owner.thread.name,
            owner.generation,
            wire_root_id,
            self.assignment.wire_seq,
            self.assignment.message_id,
            native_request_digest(prompt),
            self.execution_id,
            self.attempt_ordinal,
        ):
            raise IdentityConflict("native send differs from its durable prompt binding")


class TriageNativeSend(NativeSendStage):
    stage: ClassVar[str] = "triage"
    execution_id: ClassVar[None] = None
    attempt_ordinal: ClassVar[None] = None

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
