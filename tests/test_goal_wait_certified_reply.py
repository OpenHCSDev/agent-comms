"""Dependency replies retain original checked identities across registry changes."""

from agent_comms.messages import NoticeRole
import os
from dataclasses import replace


from agent_comms.child_process import ProcessIdentity
from agent_comms.goal_actions import GoalPrecondition, SetGoalAction, StandbyGoalAction
from agent_comms.goal_presentation import GoalWaitTarget
from goal_owner_fixture import canonical_goal_wire
from agent_comms.goal_waits import NoGoalWait


def register(comms, name):
    comms.registry.declare(
        comms.registry.new_thread(name, frozenset(), str(comms.root), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    return comms.registry.require(name)


def test_first_wait_ignores_unreserved_reply_before_goal_publication(tmp_path):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.goals import Goal
    from agent_comms.input_disposition import InputDispositions

    comms = canonical_goal_wire(tmp_path / "wire")
    register(comms, "owner")
    register(comms, "peer")
    old = comms.messaging.send("peer", "owner", "Before this goal").message
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Wait for later work"))
    assert goal.published_seq == old.seq
    assert FieldCodec.decode(Goal, goal.to_wire()) == goal
    assert list(comms.goals.goal_input_review("owner", goal.id, ["peer"]).reviewed_inputs) == []
    # A recorded UNKNOWN remains reviewable even before the goal boundary.
    inputs = InputDispositions(comms.root)
    inputs.record(
        f"bus:{old.seq}", seq=old.seq, owner="owner", target="owner", text=old.body,
        claim_scope=comms.registry.snapshot().claim_generation("owner"),
    )
    assert list(comms.goals.goal_input_review("owner", goal.id, ["peer"]).reviewed_inputs) == [
        f"bus:{old.seq}"
    ]


def test_first_wait_reviews_reply_after_goal_publication(tmp_path):
    comms = canonical_goal_wire(tmp_path / "wire")
    register(comms, "owner")
    register(comms, "peer")
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Wait for later work"))
    reply = comms.messaging.send("peer", "owner", "After this goal").message
    assert reply.seq > goal.published_seq
    assert list(comms.goals.goal_input_review("owner", goal.id, ["peer"]).reviewed_inputs) == [
        f"bus:{reply.seq}"
    ]


def test_thread_without_goal_has_no_wait(tmp_path):
    comms = canonical_goal_wire(tmp_path / "wire")
    register(comms, "owner")
    assert isinstance(comms.goals.goal_wait("owner"), NoGoalWait)


def test_original_reply_is_not_reassigned_to_same_name_replacement(tmp_path):
    comms = canonical_goal_wire(tmp_path / "wire")
    owner = register(comms, "owner")
    peer = register(comms, "peer")
    lease = comms.agents.begin_turn("peer", "original-work").turn_lease
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Wait for original peer"))
    comms.goals.update_goal(
        "owner", StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("peer",))
    )
    wait = comms.goals.goal_wait("owner")
    reply = comms.messaging.send("peer", "owner", "Original dependency reply").message
    with comms.bus.bus.reading() as source:
        original = source.lookup(reply.seq)
        assert wait.matches(original, owner.incarnation.require_recorded())
        assert original.audience.sender_lookup == peer.incarnation.lookup

    comms.agents.finish_turn(lease)
    comms.registry.unregister("peer")
    comms.registry.remove("peer")
    replacement = register(comms, "peer")
    assert peer.created_at != replacement.created_at
    with comms.bus.bus.reading() as source:
        original = source.lookup(reply.seq)
        assert wait.matches(original, owner.incarnation.require_recorded())
        replacement_wait = replace(
            wait, targets=(GoalWaitTarget(replacement.incarnation.require_recorded()),)
        )
        assert not replacement_wait.matches(original, owner.incarnation.require_recorded())
    assert wait.has_reply(comms.bus.bus, owner.incarnation.require_recorded())
    assert not replacement_wait.has_reply(comms.bus.bus, owner.incarnation.require_recorded())
    review = comms.goals.goal_input_review("owner", goal.id, ["peer"])
    assert list(review.reviewed_inputs) == []


def test_original_recipient_and_direct_reply_semantics_cannot_be_replaced(tmp_path):
    comms = canonical_goal_wire(tmp_path / "wire")
    owner = register(comms, "owner")
    register(comms, "other")
    register(comms, "peer")
    comms.agents.begin_turn("peer", "original-work")
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Wait for original owner reply"))
    comms.goals.update_goal(
        "owner", StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("peer",))
    )
    wait = comms.goals.goal_wait("owner")
    wrong = comms.messaging.send("peer", "other", "Different addressed recipient").message
    notice = comms.messaging.send("peer", "owner", "Diagnostic only", role=NoticeRole()).message
    reply = comms.messaging.send("peer", "owner", "Original addressed reply").message
    with comms.bus.bus.reading() as source:
        assert not wait.matches(source.lookup(wrong.seq), owner.incarnation.require_recorded())
        assert not wait.matches(source.lookup(notice.seq), owner.incarnation.require_recorded())
        original = source.lookup(reply.seq)
        assert wait.matches(original, owner.incarnation.require_recorded())
        assert not replace(wait, after_seq=reply.seq).matches(original, owner.incarnation.require_recorded())

    comms.registry.rename("owner", "renamed-owner")
    comms.registry.rename("peer", "renamed-peer")
    assert wait.has_reply(comms.bus.bus, owner.incarnation.require_recorded())
    assert comms.goals.goal_input_review("renamed-owner", goal.id, ["renamed-peer"]).reviewed_inputs == (
        f"bus:{reply.seq}",)
    comms.registry.unregister("renamed-owner")
    comms.registry.remove("renamed-owner")
    replacement = register(comms, "owner")
    assert owner.created_at != replacement.created_at
    with comms.bus.bus.reading() as source:
        assert not replace(wait, owner=replacement.incarnation.require_recorded()).matches(
            source.lookup(reply.seq), replacement.incarnation.require_recorded()
        )
    assert not replace(wait, owner=replacement.incarnation.require_recorded()).has_reply(
        comms.bus.bus, replacement.incarnation.require_recorded()
    )


