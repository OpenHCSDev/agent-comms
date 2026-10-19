"""The existing delivery audience owns whether work may be interrupted."""

import pytest

from agent_comms.wake_policy import MessageAudience


@pytest.mark.parametrize(
    ("audience", "joins"),
    [(MessageAudience.DIRECT, True), (MessageAudience.MENTIONED, True),
     (MessageAudience.COLLECTIVE, False)],
)
def test_message_audience_joins_active_turn(audience, joins):
    assert audience.joins_active_turn() is joins
