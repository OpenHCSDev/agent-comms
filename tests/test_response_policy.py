from dataclasses import dataclass

from agent_comms import Message, MessageType, ResponsePolicy, Thread
from agent_comms.declarations import ResponseEligibility, ScheduledTurn
from agent_comms.input_disposition import InputDispositions


def test_golden_policy_names():
    assert ResponsePolicy.names() == ("direct", "collective", "mentioned_only", "informational")


def test_new_policy_derives_all_five_consumers_without_new_cases(tmp_path):
    class ReviewPolicy(ResponsePolicy):
        starts_turn = True
        separate_turn = True

        def recipients(self, message, audience, *, aliases=None):
            return tuple(name for name in audience if name.startswith("reviewer"))

        def guidance(self, message, *, aliases=None):
            return "review; only reviewers respond"

        def disposition_key(self, message, owner):
            return f"review:{message.seq}:{owner.name}"

    try:
        policy = ReviewPolicy()

        @dataclass(frozen=True)
        class ReviewMessage(Message):
            @property
            def response_policy(self):
                return policy

        message = ReviewMessage("sender", "#review", "inspect", MessageType.INFO, seq=17)
        assert ResponseEligibility("review", ("reviewer1",)).policy.value == "review"
        assert message.response_eligibility(("reader", "reviewer1")).recipients == ("reviewer1",)
        assert message.starts_turn and message.starts_turn_for("reviewer1")
        assert not message.starts_turn_for("reader")
        turn = ScheduledTurn.incoming(message)
        assert "review; only reviewers respond" in turn.prompt
        assert ScheduledTurn.take_batch([turn, turn]) == ([turn], [turn])
        assert (
            InputDispositions.bus_key(message, Thread("reviewer1", frozenset(), str(tmp_path)))
            == "review:17:reviewer1"
        )
    finally:
        del ResponsePolicy.__registry__["review"]
