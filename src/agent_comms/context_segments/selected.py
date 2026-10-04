"""Original selected-turn instruction contributors, acquired by the family."""
from __future__ import annotations
from dataclasses import dataclass
from ..selected_participant import SelectedParticipant
from ..selected_triage import SelectedTriage
from ..turn_context import InstructionFile, InstructionSegment

@dataclass(frozen=True, kw_only=True)
class SelectedTriageSegment(InstructionSegment):
    participant: SelectedParticipant
    output: InstructionFile

    @classmethod
    def capture(cls, participant, provenance):
        instruction = InstructionFile.read("selected-triage.md")
        output = InstructionFile.read("selected-triage-output.md")
        return cls(
            provenance=(*provenance, instruction.source, output.source),
            instruction=instruction,
            participant=participant,
            output=output,
        )

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
        instruction = InstructionFile.read("selected-work.md")
        originals = action.instruction_files
        return cls(
            provenance=(*provenance, instruction.source, *(item.source for item in originals)),
            instruction=instruction,
            participant=participant,
            action_instructions=originals,
        )

    def values(self):
        return dict(
            name=self.participant.owner.thread.name,
            action=" ".join(item.content for item in self.action_instructions),
        )
