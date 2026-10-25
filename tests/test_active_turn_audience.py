"""The existing delivery audience owns whether work may be interrupted."""

import pytest

from agent_comms.thread_identity import AgentThreadRole, UserThreadRole
from agent_comms.wake_policy import MessageAudience


@pytest.mark.parametrize(
    ("audience", "joins"),
    [(MessageAudience.DIRECT, True), (MessageAudience.MENTIONED, True),
     (MessageAudience.COLLECTIVE, False)],
)
def test_message_audience_joins_active_turn(audience, joins):
    assert audience.joins_active_turn() is joins


@pytest.mark.parametrize(
    ("role", "audience"),
    [(AgentThreadRole, MessageAudience.COLLECTIVE), (UserThreadRole, MessageAudience.MENTIONED)],
)
def test_sender_role_addresses_channel_audience(role, audience):
    """The owner's channel message joins every running turn; an agent's chatter waits."""
    assert role.addressed(MessageAudience.COLLECTIVE) is audience
