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

from agent_comms.lifecycle_capability import Complete

from agent_comms.coordination_tables.assignments import ExecutionAssignmentLink, WakeAssignment
from agent_comms.coordination_tables.executions import ExecutionRecord
from agent_comms.coordination_tables.responses import ResponseObligation

from ..cohort_schema import (
    AwarenessClaimGenerations,
    ClaimBatchAcknowledgements,
    ClaimBatchMembers,
    CohortDeliveries,
    SelectedClaim,
)
from ..field_codec import FieldCodec, ThreadLookup
from ..turn_context import ContextSegment, InstructionFile, InstructionSegment, TurnContext
from ..wake_candidate_index import ProjectionUnavailableError


@dataclass(frozen=True)
class _SelectedDecision:
    generation: AwarenessClaimGenerations
    assignment: WakeAssignment
    acknowledgement: ClaimBatchAcknowledgements

    @classmethod
    def capture(
        cls,
        assignment: WakeAssignment,
        acknowledgement: ClaimBatchAcknowledgements,
        member: ClaimBatchMembers,
        delivery: CohortDeliveries,
        generation: AwarenessClaimGenerations,
    ) -> _SelectedDecision:
        acknowledgement.require_selected_claim(assignment)
        expected_member = ClaimBatchMembers(
            wire_root_id=acknowledgement.wire_root_id,
            wire_seq=assignment.wire_seq,
            ordinal=member.ordinal,
            claim_id=assignment.assignment_id,
            recipient_lookup=assignment.recipient_lookup,
        )
        if member != expected_member:
            raise ProjectionUnavailableError("selected source is not the recorded batch member")
        expected_delivery = CohortDeliveries(
            wire_root_id=acknowledgement.wire_root_id,
            wire_seq=assignment.wire_seq,
            ordinal=delivery.ordinal,
            recipient_lookup=assignment.recipient_lookup,
            canonical_thread=assignment.recipient,
            kind="selected",
            claim_id=SelectedClaim(assignment.assignment_id),
        )
        if delivery != expected_delivery:
            raise ProjectionUnavailableError("selected source delivery changed")
        generation.require_selection(assignment, acknowledgement)
        return cls(generation, assignment, acknowledgement)

    @property
    def candidate(self):
        return (
            self.assignment.wire_seq,
            self.assignment.message_id,
            type(self.assignment.lifecycle.mode),
            self.acknowledgement.exact_target,
        )

    def context(self):
        return {
            "source_seq": self.assignment.wire_seq,
            "message_id": self.assignment.message_id,
            "claim_id": self.assignment.assignment_id,
            "wake_mode": type(self.assignment.lifecycle.mode),
            "disposition": type(self.assignment.lifecycle),
            "target": self.acknowledgement.exact_target,
        }


@dataclass(frozen=True)
class _OpenObligation:
    generation: AwarenessClaimGenerations
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
        generation.require_obligation(execution, link, assignment, obligation)
        return cls(generation, obligation)

    def context(self):
        return {
            "execution_id": self.obligation.execution_id,
            "target": self.obligation.exact_target,
            "state": self.obligation.lifecycle.declared_name,
        }


class OptionalAwarenessResult(ABC):
    """Read-only context owns presentation; it never grants an action."""

    def segments(self, max_text_bytes: int) -> tuple[ContextSegment, ...]:
        self.render(max_text_bytes)
        return ()

    @abstractmethod
    def render(self, max_text_bytes: int) -> str: ...


@dataclass(frozen=True, kw_only=True)
class OmittedAwareness(InstructionSegment, OptionalAwarenessResult):
    """The named visible state of a delivery whose optional awareness dropped.

    The original is delivered alone and the omission is stated on the delivered
    input itself, like the bus-level unavailable-awareness state, never only a
    log line.
    """

    reason: str

    @classmethod
    def instruction_name(cls):
        return "selected-awareness-omitted.md"

    def values(self) -> dict[str, object]:
        return {"reason": self.reason}

    def segments(self, max_text_bytes: int) -> tuple[ContextSegment, ...]:
        # The instruction file owns the omission wording; the log and the
        # delivered input derive from that one declaration.
        logging.getLogger(__name__).warning(self.text().strip())
        return (self,)

    def render(self, max_text_bytes: int) -> str:
        return self.text()


@dataclass(frozen=True)
class CompleteAwareness(Complete, ContextSegment, OptionalAwarenessResult):
    recipient_lookup: ThreadLookup
    through_seq: int
    selected: tuple[_SelectedDecision, ...]
    open_obligations: tuple[_OpenObligation, ...]
    omitted_count: int
    instruction: InstructionFile
    content_instruction: InstructionFile

    def segments(self, max_text_bytes):
        if len(self.text().encode("utf-8")) > max_text_bytes:
            return OmittedAwareness.capture(
                reason="rendered awareness exceeds the resource budget"
            ).segments(max_text_bytes)
        return (self,)

    def context(self) -> dict:
        return {
            "recipient_lookup": self.recipient_lookup,
            "through_seq": self.through_seq,
            "selected": [row.context() for row in self.selected],
            "open_obligations": [row.context() for row in self.open_obligations],
        }

    def require_resource_budget(self, max_text_bytes: int) -> None:
        text = FieldCodec.compact(FieldCodec.encode(self.context()), ensure_ascii=False, sort_keys=True)
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
