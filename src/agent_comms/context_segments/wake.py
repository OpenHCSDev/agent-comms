"""Read-only N/K wake framing from an already committed selected acknowledgement.

The frame tells a one-shot recipient what this wake is for. It grants no
response or file-write authority: the runner separately verifies the sealed
claim, reserves native input, and fences response publication. It deliberately
has no bus/registry/SQLite reads, inbox cursor, or model-launch operation.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.presentation import SentMessageOutcome

from ..bus_publication import Delivered
from ..threads import Thread
from ..turn_context import (
    InstructionFile,
    InstructionSegment,
    NextContextTurn,
    ContextSegment,
    UserInputSegment,
    WireProvenance,
    WireSources,
)
from ..wake_policy import WakePolicy
from .wake_values import WakeWorkContext


@dataclass(frozen=True, kw_only=True)
class SelectedWakeSegment(WireSources, InstructionSegment):
    """Original sealed sources and their captured owner, never wake authority."""

    sources: tuple[tuple[Delivered, WakeAssignment], ...]
    owner: Thread
    obligations: tuple[ResponseObligation, ...]
    relevance: InstructionFile
    sent_outcomes: tuple[SentMessageOutcome, ...]
    work_context: WakeWorkContext

    sent_limit = 5

    @classmethod
    def capture(cls, sources, owner, goals, *, obligations=(), sent_outcomes=()):
        relevance = WakePolicy.relevance_instruction()
        sent_outcomes = tuple(sent_outcomes)
        if len(sent_outcomes) > cls.sent_limit:
            raise ValueError("Selected wake sent outcomes exceed their bounded window")
        return super().capture(
            (
                relevance.source,
                NextContextTurn().provenance(owner),
                *(WireProvenance(outcome.message.reference) for outcome in sent_outcomes),
            ),
            sources=tuple(sources),
            owner=owner,
            obligations=tuple(obligations),
            relevance=relevance,
            sent_outcomes=sent_outcomes,
            work_context=owner.wake_context(goals),
        )

    def input_segments(self) -> tuple[ContextSegment, ...]:
        return (self, *(MessageDelivery(source, assignment, self.owner).segment()
                        for source, assignment in self.sources))

    def values(self):
        sources, owner = self.sources, self.owner
        obligations, relevance = self.obligations, self.relevance
        selected = []
        for initial, assignment in sources:
            assignment.require_selected_source(initial, owner)
            matching = tuple(
                obligation
                for obligation in obligations
                if obligation.exact_target == assignment.lifecycle.exact_target
            )
            if len(matching) > 1:
                from ..coordination_errors import IdentityConflict

                raise IdentityConflict("Selected source has ambiguous response obligations")
            expectation, obligation_line = assignment.lifecycle.wake_frame(
                initial.message, next(iter(matching), None)
            )
            selected.append(
                {
                    "source_seq": assignment.wire_seq,
                    "claim_id": assignment.assignment_id,
                    "sender": initial.message.sender,
                    "target": initial.message.target,
                    "audience": assignment.audience.value,
                    "wake_mode": assignment.lifecycle.mode.declared_name,
                    "expectation": expectation,
                    "response_obligation": obligation_line,
                }
            )
        selected_line = FieldCodec.compact(selected, ensure_ascii=True)
        work_context = FieldCodec.compact(FieldCodec.encode(self.work_context), ensure_ascii=True)
        sent_values = []
        for outcome in self.sent_outcomes:
            message, reference = outcome.message, outcome.answer
            sent_values.append({
                "source_seq": message.seq,
                "target": message.target,
                "recipient": outcome.recipient,
                "state": outcome.label,
                "detail": outcome.state.detail,
                "answer": (
                    FieldCodec.encode(reference) if reference is not None else None
                ),
            })
        sent_outcomes = FieldCodec.compact(sent_values, ensure_ascii=True)
        return dict(
            count=len(selected),
            selected=selected_line,
            work_context=work_context,
            sent_outcomes=sent_outcomes,
            relevance=relevance.content,
        )


@dataclass(frozen=True, kw_only=True)
class DeliveredMessageSegment(WireSources, InstructionSegment):
    """One committed message handed to its recipient's already running turn."""

    delivery: MessageDelivery

    @classmethod
    def capture(cls, delivery: MessageDelivery):
        return super().capture(delivery.provenance, delivery=delivery)

    def values(self):
        message = self.delivery.source.message
        return dict(
            message=FieldCodec.compact(
                {
                    "source_seq": self.delivery.assignment.wire_seq,
                    "sender": message.sender,
                    "target": message.target,
                    "audience": self.delivery.assignment.audience.value,
                    "body": message.body,
                }, ensure_ascii=True,
            )
        )


@dataclass(frozen=True)
class MessageDelivery:
    """One committed original, framed by its task on either input route."""

    source: Delivered
    assignment: WakeAssignment
    owner: Thread

    def __post_init__(self) -> None:
        self.assignment.require_selected_source(self.source, self.owner)

    @property
    def provenance(self) -> tuple[WireProvenance, ...]:
        return (WireProvenance(self.source.message.reference),)

    def segment(self) -> ContextSegment:
        return self.source.message.task.message_segment(self)

    def delivered(self) -> DeliveredMessageSegment:
        """A bus message: framed with its sender and route, and its contents untrusted."""
        return DeliveredMessageSegment.capture(self)

    def typed(self) -> UserInputSegment:
        """The owner's original words once, without a bus or instruction wrapper."""
        return UserInputSegment(provenance=self.provenance, content=self.source.message.body)
