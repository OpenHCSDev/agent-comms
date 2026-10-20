"""The wording comparison calls the configured real owners, without fake replies."""

from dataclasses import replace

from agent_comms.commander_parity import UnverifiedParity
from agent_comms.commander_parity.wording import (
    OrderWordingComparison,
    PlainWordsComparison,
    WordingComparison,
)


def test_live_plain_word_comparison_observes_both_owners():
    result = PlainWordsComparison.read()
    assert isinstance(result, WordingComparison)
    assert result.observations
    assert result.passed
    current, native = result.observations[0]
    changed = replace(native, notes=native.notes + "a different native warning")
    assert not replace(result, observations=((current, changed),)).passed
    assert not replace(result, observations=()).passed


def test_native_order_boundary_disagreement_cannot_pass():
    result = OrderWordingComparison.read()
    if isinstance(result, UnverifiedParity):
        # Live inputs may leave the bounded tail; absence must never manufacture a pass.
        assert not result.passed
        assert result.describe() == "No actual soft-worded message exists for a refusal comparison."
    else:
        assert isinstance(result, WordingComparison)
        assert result.observations
        assert result.passed == all(current == native for current, native in result.observations)
        assert "ProblemOrderGoalAction.request_ref" in result.describe()
