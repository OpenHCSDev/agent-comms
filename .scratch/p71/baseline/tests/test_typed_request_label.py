"""A message the user types into a thread's own view says so in the turn text."""
from agent_comms.turn_context import TurnContext, UserFollowupSegment, UserRequestSegment


def test_a_typed_message_that_starts_a_turn_is_labelled_as_addressed_to_the_agent():
    text = TurnContext.render_segments((UserRequestSegment.capture("hey boss"),)).text
    assert text.endswith("\nhey boss")
    assert "typed directly to you" in text
    assert "not a channel or bus message" in text


def test_a_typed_followup_keeps_its_own_label():
    text = TurnContext.render_segments((UserFollowupSegment.capture("and this"),)).text
    assert text == "User follow-up:\nand this"

