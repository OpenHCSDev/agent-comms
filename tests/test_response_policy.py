from agent_comms.execution.input_key import BusInputKey
from agent_comms.execution.input_key import OwnedBusInputKey
from agent_comms.messaging.messages import ConversationRole, InfoKind, Message
from agent_comms.messaging.response_policy import ResponseEligibility, ResponsePolicy
from agent_comms.messaging.routing import ScheduledTurn
from agent_comms.nominal.field_codec import GoalId
from agent_comms.planning.goal_scope import OneGoal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread


def test_golden_policy_names():
    assert ResponsePolicy.names() == ("direct", "collective", "mentioned", "informational")


def test_new_policy_drives_eligibility_guidance_and_batching(tmp_path, thread_registration):
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
        owner = NativeThread("reviewer", frozenset(), Checkout(tmp_path))
        assert message.bus_key(owner) == OwnedBusInputKey(message.seq, float(owner.created_at))

    finally:
        ReviewPolicy.withdraw()


def test_autonomous_turns_keep_message_batches_and_delivery_keys_separate(tmp_path, thread_registration):
    goal_id = GoalId.mint()

    goal = ScheduledTurn("Continue the goal", goal_id=OneGoal(goal_id))
    resume = ScheduledTurn.resuming_in_project(Checkout(tmp_path))
    first = ScheduledTurn.incoming(Message("peer", "#team", "first", InfoKind, seq=1))
    second = ScheduledTurn.incoming(Message("peer", "#team", "second", InfoKind, seq=2))
    direct = ScheduledTurn.incoming(Message("peer", "owner", "direct", InfoKind, seq=3))

    assert goal.autonomous_goal and not resume.autonomous_goal
    assert ScheduledTurn.take_batch([goal, resume, first]) == ([goal, resume], [first])
    assert ScheduledTurn.take_batch([first, second, goal]) == ([first, second], [goal])
    assert ScheduledTurn.take_batch([direct, direct]) == ([direct], [direct])

    owner = NativeThread("owner", frozenset(), Checkout(tmp_path))
    assert goal.origin.messages() == ()
    assert goal.origin.bus_keys(owner) == ()
    assert first.origin.messages() == (first.origin,)
    assert first.origin.bus_keys(owner) == (OwnedBusInputKey(first.origin.seq, float(owner.created_at)),)
    assert direct.origin.bus_keys(owner) == (BusInputKey(direct.origin.seq),)
