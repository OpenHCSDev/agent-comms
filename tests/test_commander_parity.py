from pathlib import Path

from agent_comms.commander_parity import ParityComparison, ParityRow

PLAN = Path(__file__).resolve().parents[1] / "plans/commander-native-plan.md"


class MatchingComparison(ParityComparison):
    passed = True




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
    assert not ParityRow.done(())
