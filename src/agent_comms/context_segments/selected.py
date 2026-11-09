"""Original selected-turn instruction contributors, acquired by the family."""
from __future__ import annotations

from dataclasses import dataclass

from agent_comms.execution.selected.participant import SelectedParticipant
from agent_comms.execution.selected.triage import SelectedTriage
from agent_comms.execution.turn_context import InstructionFile, InstructionSegment


@dataclass(frozen=True, kw_only=True)
class SelectedTriageSegment(InstructionSegment):
    participant: SelectedParticipant
    output: InstructionFile

    @classmethod
    def capture(cls, participant, provenance):
        output = InstructionFile.read("selected-triage-output.md")
        return super().capture((*provenance, output.source), participant=participant, output=output)

    def values(self):
        return dict(
            name=self.participant.owner.thread.name,
            output=self.output.render(SelectedTriage.output_values()),
        )


@dataclass(frozen=True, kw_only=True)
class SelectedWorkSegment(InstructionSegment):
    participant: SelectedParticipant
    action_instructions: tuple[InstructionFile, ...]

    @classmethod
    def capture(cls, participant, action, provenance):
        originals = action.instruction_files
        return super().capture((*provenance, *(item.source for item in originals)),
                               participant=participant, action_instructions=originals)

    def values(self):
        return dict(
            name=self.participant.owner.thread.name,
            action=" ".join(item.content for item in self.action_instructions),
        )
