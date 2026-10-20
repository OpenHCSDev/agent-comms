from pathlib import Path

from agent_comms.commander_parity import ParityComparison, ParityRow, UnverifiedParity
from agent_comms.plan_phases import (
    CommanderPagePhase,
    ControlPlanePhase,
    CutoverPhase,
    ParityPhase,
    PlanPhase,
    RecordPlanePhase,
    ReviewQueuePhase,
    StoryPhase,
)

PLAN = Path(__file__).resolve().parents[1] / "plans/commander-native-plan.md"


class MatchingComparison(ParityComparison):
    passed = True


def test_cutover_gate_is_computed_from_every_comparison(monkeypatch):
    assert CutoverPhase.after == (
        RecordPlanePhase,
        ReviewQueuePhase,
        ControlPlanePhase,
        ParityPhase,
    )
    assert not ParityPhase.done(())
    for row in ParityRow.rows():
        monkeypatch.setattr(row, "compare", classmethod(lambda cls, history: MatchingComparison()))
    assert ParityPhase.done(())
    last = ParityRow.rows()[-1]
    monkeypatch.setattr(last, "compare", classmethod(lambda cls, history: UnverifiedParity()))
    assert not ParityPhase.done(())
    assert not any(
        dependency in (StoryPhase, CommanderPagePhase)
        for phase in PlanPhase.members_with(PlanPhase)
        for dependency in phase.after
    )


def test_generated_prose_is_not_input_to_the_gate(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text(
        "Before\n## Parity matrix\n\nHand edited, invalid prose.\n"
        "**Dropped deliberately:** historical text\nAfter\n"
    )
    rendered = ParityRow.render_plan(plan)
    assert rendered.startswith("Before\n## Parity matrix\n\n")
    assert rendered.endswith("**Dropped deliberately:** historical text\nAfter\n")
    assert "Hand edited" not in rendered
    assert not ParityPhase.done(())
