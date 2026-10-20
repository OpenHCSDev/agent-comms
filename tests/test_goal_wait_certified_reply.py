"""Dependency replies retain original checked identities across registry changes."""

from conftest import reader
from agent_comms.messages import NoticeRole
import os
from agent_comms.tool_caller import NoThreadCaller
import asyncio
import json
from dataclasses import replace

from test_goal_standby_liveness import retained_native_acp_owner as retained_native_acp_owner

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.child_process import ProcessIdentity
from agent_comms.goal_actions import GoalPrecondition, SetGoalAction, StandbyGoalAction
from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.threads import Thread
from goal_owner_fixture import canonical_goal_wire
from stored_inputs import stored_inputs
from agent_comms.goal_waits import NoGoalWait
from agent_comms.pi_payloads import PublishedMessageToolDetails


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
        claim_scope=comms.registry.snapshot().claimed_generations["owner"],
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
    with comms.bus.log.checked_read() as source:
        original = source.delivery(reply.seq)
        assert wait.matches(original, owner.incarnation.require_recorded())
        assert original.audience.sender_lookup == RecordedThreadIncarnation("fixture", peer.created_at).lookup

    comms.agents.finish_turn(lease)
    comms.registry.unregister("peer")
    comms.registry.remove("peer")
    replacement = register(comms, "peer")
    assert peer.created_at != replacement.created_at
    with comms.bus.log.checked_read() as source:
        original = source.delivery(reply.seq)
        assert wait.matches(original, owner.incarnation.require_recorded())
        replacement_wait = replace(
            wait, targets=(GoalWaitTarget(replacement.incarnation.require_recorded()),)
        )
        assert not replacement_wait.matches(original, owner.incarnation.require_recorded())
    assert wait.has_reply(comms.bus.log, owner.incarnation.require_recorded())
    assert not replacement_wait.has_reply(comms.bus.log, owner.incarnation.require_recorded())
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
    with comms.bus.log.checked_read() as source:
        assert not wait.matches(source.delivery(wrong.seq), owner.incarnation.require_recorded())
        assert not wait.matches(source.delivery(notice.seq), owner.incarnation.require_recorded())
        original = source.delivery(reply.seq)
        assert wait.matches(original, owner.incarnation.require_recorded())
        assert not replace(wait, after_seq=reply.seq).matches(original, owner.incarnation.require_recorded())

    comms.registry.rename("owner", "renamed-owner")
    comms.registry.rename("peer", "renamed-peer")
    assert wait.has_reply(comms.bus.log, owner.incarnation.require_recorded())
    assert comms.goals.goal_input_review("renamed-owner", goal.id, ["renamed-peer"]).reviewed_inputs == (
        f"bus:{reply.seq}",)
    comms.registry.unregister("renamed-owner")
    comms.registry.remove("renamed-owner")
    replacement = register(comms, "owner")
    assert owner.created_at != replacement.created_at
    with comms.bus.log.checked_read() as source:
        assert not replace(wait, owner=replacement.incarnation.require_recorded()).matches(
            source.delivery(reply.seq), replacement.incarnation.require_recorded()
        )
    assert not replace(wait, owner=replacement.incarnation.require_recorded()).has_reply(
        comms.bus.log, replacement.incarnation.require_recorded()
    )


async def test_native(retained_native_acp_owner, *, source_reads):
    """Retained native -> real tool send -> selected reply -> queued ACP input."""
    from agent_comms.acp_extension import InputDeliveryChangedUpdate, decode_updates
    from agent_comms.coordinator import Coordination
    from agent_comms.input_disposition import InputDispositions
    from agent_comms.historical_native_inputs import read_historical_native_inputs
    from agent_comms.native_input_record import FullNativeExecution
    from agent_comms.tools import invoke_tool

    native, comms, agent, sid = retained_native_acp_owner
    await native.run("RETAINED_GOAL_WAIT_HISTORY")
    retained = native.session.read_bytes()
    assert native.provider.posts == 1
    register(comms, "peer")
    peer_lease = comms.agents.begin_turn("peer", "peer-work").turn_lease
    goal = comms.goals.update_goal(sid, SetGoalAction(text="Process the original peer reply"))
    old_key = "acp:historical-unknown"
    owner = comms.registry.require(sid)
    claim_scope = comms.registry.snapshot().claimed_generations[sid]
    agent.inputs.dispositions.record(
        old_key, seq=None, owner=sid, claim_scope=claim_scope, target=sid,
        text="HISTORICAL_UNKNOWN_MUST_NOT_REPLAY",
    )
    old = stored_inputs(agent.inputs.dispositions.root).lookup(old_key)
    comms.goals.update_goal(
        sid, StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("peer",))
    )
    wait = comms.goals.goal_wait(sid)
    parked_goal = comms.registry.require(sid).goal
    assert wait is not None and wait.owner == owner.incarnation.require_recorded()

    gate = asyncio.Event()
    native.provider.response_gate = gate
    native.provider.text = "NATIVE_GOAL_WAIT_HANDLED"
    agent.inputs.auto_wake = True
    sent = invoke_tool(comms, "comms_send", {
        "from": "peer", "to": sid, "body": "Original dependency reply",
    }, caller=NoThreadCaller())
    original, = comms.bus.log.messages_for_references((PublishedMessageToolDetails.from_wire(sent).message.source,))
    with comms.bus.log.checked_read() as source:
        assert wait.matches(source.delivery(original.seq), owner.incarnation.require_recorded())
        root_id = source.marker.root_id
    turn = asyncio.create_task(agent.inputs.drain_inbox(sid))
    try:
        await turn
        assert agent.inputs.state(sid).wake_task is not None
        wake = agent.inputs.state(sid).wake_task
        async with asyncio.timeout(15):
            while native.provider.posts < 2:
                if wake.done():
                    await wake
                    raise AssertionError("Selected wake ended before its actual native request")
                await asyncio.sleep(0.01)
        assert "Original dependency reply" in json.dumps(native.provider.requests[-1])
        assert comms.goals.goal_wait(sid) == wait
        response = await agent.prompt(sid, [{"type": "text", "text": "QUEUED_OWNER_FOLLOWUP"}])
        accepted = next(
            item for item in decode_updates(response.field_meta)
            if isinstance(item, InputDeliveryChangedUpdate)
        )
        assert accepted.input_id in agent.inputs.state(sid).queued
        queued = stored_inputs(agent.inputs.dispositions.root).lookup(f"acp:{accepted.input_id}")
        assert queued.unresolved and queued.has_native_binding
        gate.set()
        native.provider.response_gate = None
        async with asyncio.timeout(30):
            await wake
            assert await turn == 1
        assert isinstance(comms.goals.goal_wait(sid), NoGoalWait)
        assert comms.registry.require(sid).goal == parked_goal
        assert not comms.registry.require(sid).turn_record.executing
        handled = stored_inputs(agent.inputs.dispositions.root).lookup(f"acp:{accepted.input_id}")
        assert not handled.unresolved and handled.has_native_binding
        assert not agent.inputs.state(sid).queued
        assert stored_inputs(agent.inputs.dispositions.root).lookup(old_key) == old
        assert native.provider.posts == 3
        assert not any("HISTORICAL_UNKNOWN_MUST_NOT_REPLAY" in json.dumps(r) for r in native.provider.requests)
        assert "QUEUED_OWNER_FOLLOWUP" in json.dumps(native.provider.requests[-1])
        assert native.session.read_bytes().startswith(retained)
        replies = comms.bus.inbox("peer")
        assert len(replies) == 1 and replies[0].body == "NATIVE_GOAL_WAIT_HANDLED"
        with Coordination(str(comms.root / "coordination.sqlite3")) as store:
            proofs = read_historical_native_inputs(
                store,
                wire_root_id=root_id,
                recipient_lookup=RecordedThreadIncarnation(
                    "fixture", owner.created_at
                ).lookup,
                source_seq=original.seq,
                source_reads=source_reads,
            )
            assert len(proofs) == 1 and isinstance(proofs[0].execution, FullNativeExecution)
            assert proofs[0].expected_prompt_equality_established
        comms.agents.finish_turn(peer_lease)
        assert await agent.inputs.drain_inbox(sid) == 0
        assert native.provider.posts == 3
        reopened = reader(comms.root)
        assert isinstance(reopened.goals.goal_wait(sid), NoGoalWait)
        assert reopened.registry.require(sid).goal == parked_goal
        assert reopened.bus.log.messages_for_references((original.reference,)) == (original,)
        assert stored_inputs(comms.root).lookup(old_key) == old
    finally:
        gate.set()
        native.provider.response_gate = None
        await asyncio.gather(turn, return_exceptions=True)
