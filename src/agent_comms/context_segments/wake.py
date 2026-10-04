"""Read-only N/K wake framing from an already committed selected receipt.

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

    @classmethod
    def capture(cls, sources, owner, *, obligations=()):
        instruction = InstructionFile.read("selected-wake.md")
        relevance = WakePolicy.relevance_instruction()
        return cls(
            provenance=(
                instruction.source,
                relevance.source,
                OwnerProvenance(owner.incarnation, NextContextTurn().source_revision(owner)),
                *(WireProvenance(initial.message.reference) for initial, _ in sources),
            ),
            instruction=instruction,
            sources=tuple(sources),
            owner=owner,
            obligations=tuple(obligations),
            relevance=relevance,
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
            {
                "name": owner.name,
                "title": owner.title,
                "tags": sorted(owner.tags),
                "original_assignment": owner.task,
                "current_goal": (
                    None
                    if owner.goal is None
                    else {
                        "text": owner.goal.text,
                        "status": owner.goal.state.declared_name,
                        "progress": owner.goal.progress,
                    }
                ),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return dict(
            count=len(selected),
            selected=selected_line,
            work_context=work_context,
            relevance=relevance.content,
        )
