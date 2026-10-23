from agent_comms.messages import ConversationRole, Message, InfoKind
from agent_comms.response_policy import ResponseEligibility, ResponsePolicy
from agent_comms.routing import ScheduledTurn


def test_golden_policy_names():
    assert ResponsePolicy.names() == ("direct", "collective", "mentioned_only", "informational")


def test_new_policy_drives_eligibility_guidance_and_batching(tmp_path):
    class ReviewPolicy(ResponsePolicy):
        starts_turn = True
        separate_turn = True

        @staticmethod
        def selected(audience):
            return tuple(name for name in audience if name.startswith("reviewer"))

        def recipients(self, message, audience, *, aliases=None):
            return self.selected(audience)

        def guidance(self, message, *, aliases=None):
            return "review; only reviewers respond"

    try:
        policy = ReviewPolicy()

        class ReviewRole(ConversationRole):
            def response_policy(self, message):
                return policy

        message = Message("sender", "#review", "inspect", InfoKind, seq=17, role=ReviewRole())
        assert ResponseEligibility(policy, ("reviewer1",)).policy is policy
        assert message.response_eligibility(("reader", "reviewer1")).recipients == ("reviewer1",)
        assert message.role.response_policy(message).starts_turn and message.role.response_policy(message).starts_turn_for(message, "reviewer1")
        assert not message.role.response_policy(message).starts_turn_for(message, "reader")
        turn = ScheduledTurn.incoming(message)
        assert "review; only reviewers respond" in turn.prompt
        assert ScheduledTurn.take_batch([turn, turn]) == ([turn], [turn])

    finally:
        del ResponsePolicy.__registry__["review"]
