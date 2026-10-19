"""Read-only N/K wake framing from an already committed selected acknowledgement.

The frame tells a one-shot recipient what this wake is for. It grants no
response or file-write authority: the runner separately verifies the sealed
claim, reserves native input, and fences response publication. It deliberately
has no bus/registry/SQLite reads, inbox cursor, or model-launch operation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.field_codec import FieldCodec
from agent_comms.presentation import SentMessageOutcome

from ..bus_publication import CommittedDelivery
from ..threads import Thread
from ..turn_context import (
    InstructionFile,
    InstructionSegment,
    OwnerProvenance,
    NextContextTurn,
    WireProvenance,
)
from ..wake_policy import WakePolicy


@dataclass(frozen=True, kw_only=True)
class SelectedWakeSegment(InstructionSegment):
    """Original sealed sources and their captured owner, never wake authority."""

    sources: tuple[tuple[CommittedDelivery, WakeAssignment], ...]
    owner: Thread
    obligations: tuple[ResponseObligation, ...]
    relevance: InstructionFile
    sent_outcomes: tuple[SentMessageOutcome, ...]
    work_context: dict[str, object]

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
                OwnerProvenance(owner.incarnation, NextContextTurn().source_revision(owner)),
                *(WireProvenance(initial.message.reference) for initial, _ in sources),
                *(WireProvenance(outcome.message.reference) for outcome in sent_outcomes),
            ),
            sources=tuple(sources),
            owner=owner,
            obligations=tuple(obligations),
            relevance=relevance,
            sent_outcomes=sent_outcomes,
            work_context=owner.wake_context(goals),
        )

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
                    "body": initial.message.body,
                }
            )
        selected_line = json.dumps(
            selected,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        work_context = json.dumps(
            self.work_context,
            ensure_ascii=True,
            separators=(",", ":"),
        )
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
        sent_outcomes = json.dumps(
            sent_values,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return dict(
            count=len(selected),
            selected=selected_line,
            work_context=work_context,
            sent_outcomes=sent_outcomes,
            relevance=relevance.content,
        )


@dataclass(frozen=True, kw_only=True)
class DeliveredMessageSegment(InstructionSegment):
    """One committed message handed to its recipient's already running turn."""

    source: CommittedDelivery
    assignment: WakeAssignment
    owner: Thread

    @classmethod
    def capture(cls, source, assignment, owner):
        return super().capture((WireProvenance(source.message.reference),),
                               source=source, assignment=assignment, owner=owner)

    def values(self):
        self.assignment.require_selected_source(self.source, self.owner)
        message = self.source.message
        return dict(
            message=json.dumps(
                {
                    "source_seq": self.assignment.wire_seq,
                    "sender": message.sender,
                    "target": message.target,
                    "audience": self.assignment.audience.value,
                    "body": message.body,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
