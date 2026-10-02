from dataclasses import dataclass

from agent_comms.messages import Message, MessageType
from agent_comms.response_policy import ResponseEligibility, ResponsePolicy
from agent_comms.routing import ScheduledTurn


def test_golden_policy_names():
    assert ResponsePolicy.names() == ("direct", "collective", "mentioned_only", "informational")


def test_new_policy_drives_eligibility_guidance_and_batching(tmp_path):
    class ReviewPolicy(ResponsePolicy):
        starts_turn = True
        separate_turn = True

        def recipients(self, message, audience, *, aliases=None):
            return tuple(name for name in audience if name.startswith("reviewer"))

        def guidance(self, message, *, aliases=None):
            return "review; only reviewers respond"

    try:
        policy = ReviewPolicy()

        @dataclass(frozen=True)
        class ReviewMessage(Message):
            @property
            def response_policy(self):
                return policy

        message = ReviewMessage("sender", "#review", "inspect", MessageType.INFO, seq=17)
        assert ResponseEligibility(policy, ("reviewer1",)).policy is policy
        assert message.response_eligibility(("reader", "reviewer1")).recipients == ("reviewer1",)
        assert message.starts_turn and message.starts_turn_for("reviewer1")
        assert not message.starts_turn_for("reader")
        turn = ScheduledTurn.incoming(message)
        assert "review; only reviewers respond" in turn.prompt
        assert ScheduledTurn.take_batch([turn, turn]) == ([turn], [turn])

    finally:
        del ResponsePolicy.__registry__["review"]
