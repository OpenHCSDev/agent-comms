"""A stalled phase stops the commander when it crosses the window and at each doubling."""

import pytest

from agent_comms.commander.standing import PlanStalled
from agent_comms.commander.vibe_check import (
    ChokeholdUnanswered,
    Escalation,
    PlanStalledDegradation,
    VibeCheckUnanswered,
)

HOUR = 3600


@pytest.mark.parametrize("held, doublings", [
    (2 * HOUR, 0), (3.9 * HOUR, 0), (4 * HOUR, 1), (7.9 * HOUR, 1), (8 * HOUR, 2), (17 * HOUR, 3),
])
def test_a_stall_is_new_again_only_when_its_age_doubles(held, doublings):
    assert PlanStalled.window == 2 * HOUR
    assert PlanStalledDegradation.doublings(held) == doublings


def test_answers_owed_to_escalations_never_feed_a_new_vibe_check():
    assert issubclass(VibeCheckUnanswered, Escalation)
    assert issubclass(ChokeholdUnanswered, Escalation)
