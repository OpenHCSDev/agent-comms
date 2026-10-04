"""Bounded, read-only awareness of sealed private work for one selected owner.

The candidate WAL locates changed bus rows. It never proves a claim, response
obligation, native input, or permission to act. The caller supplies the already
verified current original, selected claim, and live owner-turn snapshot; the
native send boundary must recheck that owner after this optional read.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from agent_comms.coordination_tables.assignments import ExecutionAssignmentLink, WakeAssignment
from agent_comms.coordination_tables.executions import ExecutionOrigin, ExecutionRecord
from agent_comms.coordination_tables.participants import OwnerGenerations
from agent_comms.coordination_tables.responses import ResponseObligation

from ..cohort_schema import AwarenessClaimGenerations, ClaimBatchMembers, ClaimBatchReceipts, CohortDeliveryReceipts
from ..field_codec import FieldCodec
from ..turn_context import ContextSegment, InstructionFile, TurnContext
from ..wake_candidate_index import ProjectionUnavailableError


@dataclass(frozen=True)
class _GenerationProvenance:
    """The SQL owner's captured generation, not a present-day wake grant."""

    generation: AwarenessClaimGenerations

    def current(self, expected: OwnerGenerations) -> bool:
        captured = OwnerGenerations(
            owner_lookup=self.generation.recipient_lookup,
            owner_thread=self.generation.canonical_thread,
            generation=self.generation.owner_generation,
        )
        if captured.generation < expected.generation:
            return False
        if captured != expected:
            raise ProjectionUnavailableError("captured participant owner changed")
        return True


@dataclass(frozen=True)
class _SelectedDecision(_GenerationProvenance):
    assignment: WakeAssignment
    receipt: ClaimBatchReceipts

    @classmethod
    def capture(
        cls,
        assignment: WakeAssignment,
        receipt: ClaimBatchReceipts,
        member: ClaimBatchMembers,
        delivery: CohortDeliveryReceipts,
        generation: AwarenessClaimGenerations,
    ) -> _SelectedDecision:
        if not receipt.sealed or receipt.message_id != assignment.message_id:
            raise ProjectionUnavailableError("selected source receipt is unsealed or changed")
        expected_member = ClaimBatchMembers(
            wire_root_id=receipt.wire_root_id,
            wire_seq=assignment.wire_seq,
            ordinal=member.ordinal,
            claim_id=assignment.assignment_id,
            recipient_lookup=assignment.recipient_lookup,
        )
        if member != expected_member:
            raise ProjectionUnavailableError("selected source is not the recorded batch member")
        expected_delivery = CohortDeliveryReceipts(
            wire_root_id=receipt.wire_root_id,
            wire_seq=assignment.wire_seq,
            ordinal=delivery.ordinal,
            recipient_lookup=assignment.recipient_lookup,
            canonical_thread=assignment.recipient,
            kind="selected",
            claim_id=assignment.assignment_id,
        )
        if delivery != expected_delivery:
            raise ProjectionUnavailableError("selected source delivery changed")
        expected_generation = AwarenessClaimGenerations(
            claim_id=assignment.assignment_id,
            wire_root_id=receipt.wire_root_id,
            wire_seq=assignment.wire_seq,
            recipient_lookup=assignment.recipient_lookup,
            canonical_thread=assignment.recipient,
            owner_generation=generation.owner_generation,
        )
        if generation != expected_generation:
            raise ProjectionUnavailableError("selected source generation provenance changed")
        return cls(generation, assignment, receipt)

    @property
    def candidate(self):
        return (
            self.assignment.wire_seq,
            self.assignment.message_id,
            type(self.assignment.lifecycle.mode),
            self.receipt.exact_target,
        )

    def context(self):
        return {
            "source_seq": self.assignment.wire_seq,
            "message_id": self.assignment.message_id,
            "claim_id": self.assignment.assignment_id,
            "wake_mode": type(self.assignment.lifecycle.mode),
            "disposition": type(self.assignment.lifecycle),
            "target": self.receipt.exact_target,
        }


@dataclass(frozen=True)
class _OpenObligation(_GenerationProvenance):
    obligation: ResponseObligation

    @classmethod
    def capture(
        cls,
        obligation: ResponseObligation,
        execution: ExecutionRecord,
        link: ExecutionAssignmentLink,
        generation: AwarenessClaimGenerations,
        assignment: WakeAssignment,
    ) -> _OpenObligation:
        if execution.origin is not ExecutionOrigin.WIRE:
            raise ProjectionUnavailableError("open obligation has no wire origin")
        expected = OwnerGenerations(
            owner_lookup=execution.owner_lookup,
            owner_thread=execution.owner_thread,
            generation=generation.owner_generation,
        )
        captured = OwnerGenerations(
            owner_lookup=generation.recipient_lookup,
            owner_thread=generation.canonical_thread,
            generation=generation.owner_generation,
        )
        if (
            captured != expected or link.assignment_id != generation.claim_id
            or link.assignment_id != assignment.assignment_id
            or link.execution_id != obligation.execution_id
            or assignment.lifecycle.exact_target != obligation.exact_target
        ):
            raise ProjectionUnavailableError("open obligation has no exact owner provenance")
        return cls(generation, obligation)

    def context(self):
        return {
            "execution_id": self.obligation.execution_id,
            "target": self.obligation.exact_target,
            "state": self.obligation.lifecycle.declared_name,
        }


class OptionalAwarenessResult(ABC):
    """Read-only context owns presentation; it never grants an action."""

    complete = False

    def segments(self, max_text_bytes: int) -> tuple[ContextSegment, ...]:
        self.render(max_text_bytes)
        return ()

    @abstractmethod
    def render(self, max_text_bytes: int) -> str: ...


@dataclass(frozen=True)
class OmittedAwareness(OptionalAwarenessResult):
    reason: str

    def render(self, max_text_bytes: int) -> str:
        logging.getLogger(__name__).warning(
            "Optional awareness omitted; original delivered alone (%s)", self.reason
        )
        return ""


@dataclass(frozen=True)
class CompleteAwareness(ContextSegment, OptionalAwarenessResult):
    recipient_lookup: str
    through_seq: int
    selected: tuple[_SelectedDecision, ...]
    open_obligations: tuple[_OpenObligation, ...]
    omitted_count: int
    instruction: InstructionFile
    content_instruction: InstructionFile
    complete = True

    def segments(self, max_text_bytes):
        if len(self.text().encode("utf-8")) > max_text_bytes:
            return OmittedAwareness("rendered awareness exceeds the resource budget").segments(
                max_text_bytes
            )
        return (self,)

    def context(self) -> dict:
        return {
            "recipient_lookup": self.recipient_lookup,
            "through_seq": self.through_seq,
            "selected": [row.context() for row in self.selected],
            "open_obligations": [row.context() for row in self.open_obligations],
        }

    def require_resource_budget(self, max_text_bytes: int) -> None:
        text = json.dumps(FieldCodec.encode(self.context()), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(text.encode("utf-8")) > max_text_bytes:
            raise ProjectionUnavailableError("binding awareness exceeds the byte budget")

    def text(self) -> str:
        content = self.content_instruction.render(
            dict(
                sequence=self.through_seq,
                selected=json.dumps(
                    FieldCodec.encode([row.context() for row in self.selected]), ensure_ascii=False, sort_keys=True
                ),
                obligations=json.dumps(
                    FieldCodec.encode([row.context() for row in self.open_obligations]),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        return self.instruction.render(
            dict(content=json.dumps(content, ensure_ascii=False), omitted=self.omitted_count)
        )

    def render(self, max_text_bytes: int) -> str:
        return TurnContext.render_segments(self.segments(max_text_bytes)).text
