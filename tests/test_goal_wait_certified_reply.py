"""Dependency replies retain original certified identities across registry changes."""

import os
import asyncio
import json
from dataclasses import replace

from test_goal_standby_liveness import retained_native_acp_owner as retained_native_acp_owner
from test_backend_native_lifecycle import native_backend as native_backend

from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.goal_actions import GoalPrecondition, SetGoalAction, StandbyGoalAction
from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.threads import Thread
from goal_owner_fixture import canonical_goal_wire


def register(comms, name):
    comms.registry.declare(
        Thread(name, frozenset(), str(comms.root), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    return comms.registry.require(name)


def test_original_reply_is_not_reassigned_to_same_name_replacement(tmp_path):
    comms = canonical_goal_wire(tmp_path / "wire")
    register(comms, "owner")
    peer = register(comms, "peer")
    lease = comms.agents.begin_turn("peer", "original-work")
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Wait for original peer"))
    comms.goals.update_goal(
        "owner", StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("peer",))
    )
    wait = comms.goals.goal_wait("owner")
    reply = comms.messaging.send_message("peer", "owner", "Original dependency reply")
    with comms.bus.log.certified_read() as source:
        original = source.delivery(reply.seq)
        assert wait.matches(original)
        assert original.audience.sender_lookup == stable_thread_lookup(peer.created_at)

    comms.agents.finish_turn(lease)
    comms.registry.unregister("peer")
    comms.registry.remove("peer")
    replacement = register(comms, "peer")
    assert peer.created_at != replacement.created_at
    with comms.bus.log.certified_read() as source:
        original = source.delivery(reply.seq)
        assert wait.matches(original)
        replacement_wait = replace(
            wait, targets=(GoalWaitTarget(replacement.name, replacement.created_at),)
        )
        assert not replacement_wait.matches(original)
    assert wait.has_reply(comms.bus.log)
    assert not replacement_wait.has_reply(comms.bus.log)
    review = comms.goals.goal_input_review("owner", goal.id, ["peer"])
    assert review["reviewed_inputs"] == []


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
    wrong = comms.messaging.send_message("peer", "other", "Different addressed recipient")
    notice = comms.messaging.send_message("peer", "owner", "Diagnostic only", notice=True)
    reply = comms.messaging.send_message("peer", "owner", "Original addressed reply")
    with comms.bus.log.certified_read() as source:
        assert not wait.matches(source.delivery(wrong.seq))
        assert not wait.matches(source.delivery(notice.seq))
        original = source.delivery(reply.seq)
        assert wait.matches(original)
        assert not replace(wait, after_seq=reply.seq).matches(original)

    comms.registry.rename("owner", "renamed-owner")
    comms.registry.rename("peer", "renamed-peer")
    assert wait.has_reply(comms.bus.log)
    assert comms.goals.goal_input_review("renamed-owner", goal.id, ["renamed-peer"])[
        "reviewed_inputs"
    ] == [f"bus:{reply.seq}"]
    comms.registry.unregister("renamed-owner")
    comms.registry.remove("renamed-owner")
    replacement = register(comms, "owner")
    assert owner.created_at != replacement.created_at
    with comms.bus.log.certified_read() as source:
        assert not replace(wait, owner_created_at=replacement.created_at).matches(source.delivery(reply.seq))
    assert not replace(wait, owner_created_at=replacement.created_at).has_reply(comms.bus.log)


async def test_native(retained_native_acp_owner):
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
    peer_lease = comms.agents.begin_turn("peer", "peer-work")
    goal = comms.goals.update_goal(sid, SetGoalAction(text="Process the original peer reply"))
    old_key = "acp:historical-unknown"
    owner = comms.registry.require(sid)
    admission = comms.registry.snapshot().admission_generations[sid]
    agent.inputs.dispositions.record(
        old_key, seq=None, owner=sid, admission=admission, target=sid,
        text="HISTORICAL_UNKNOWN_MUST_NOT_REPLAY",
    )
    old = agent.inputs.dispositions.read().lookup(old_key)
    comms.goals.update_goal(
        sid, StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("peer",))
    )
    wait = comms.goals.goal_wait(sid)
    parked_goal = comms.registry.require(sid).goal
    assert wait is not None and wait.owner_created_at == owner.created_at

    gate = asyncio.Event()
    native.provider.response_gate = gate
    native.provider.text = "NATIVE_GOAL_WAIT_HANDLED"
    agent.inputs.auto_wake = True
    sent = invoke_tool(comms, "comms_send", {
        "from": "peer", "to": sid, "body": "Original dependency reply",
    })
    original = comms.bus.log.message_by_id(sent["id"])
    with comms.bus.log.certified_read() as source:
        assert wait.matches(source.delivery(original.seq))
        root_id = source.marker.root_id
    turn = asyncio.create_task(agent.inputs.drain_inbox(sid))
    try:
        async with asyncio.timeout(15):
            while native.provider.posts < 2:
                if turn.done():
                    await turn
                    raise AssertionError("Selected wake ended before its actual native request")
                await asyncio.sleep(0.01)
        assert "Original dependency reply" in json.dumps(native.provider.requests[-1])
        assert comms.goals.goal_wait(sid) == wait
        response = await agent.prompt(sid, [{"type": "text", "text": "QUEUED_OWNER_FOLLOWUP"}])
        accepted = next(
            item for item in decode_updates(response.field_meta)
            if isinstance(item, InputDeliveryChangedUpdate)
        )
        assert accepted.input_id in agent.inputs.queued_inputs[sid]
        queued = agent.inputs.dispositions.read().lookup(f"acp:{accepted.input_id}")
        assert queued.unresolved and not queued.has_native_binding
        gate.set()
        native.provider.response_gate = None
        async with asyncio.timeout(30):
            assert await turn == 1
        assert comms.goals.goal_wait(sid) is None
        assert comms.registry.require(sid).goal == parked_goal
        assert comms.registry.require(sid).active_turn is None
        handled = agent.inputs.dispositions.read().lookup(f"acp:{accepted.input_id}")
        assert not handled.unresolved and handled.has_native_binding
        assert not agent.inputs.queued_inputs.get(sid)
        assert agent.inputs.dispositions.read().lookup(old_key) == old
        assert native.provider.posts == 3
        assert not any("HISTORICAL_UNKNOWN_MUST_NOT_REPLAY" in json.dumps(r) for r in native.provider.requests)
        assert "QUEUED_OWNER_FOLLOWUP" in json.dumps(native.provider.requests[-1])
        assert native.session.read_bytes().startswith(retained)
        replies = comms.bus.inbox("peer")
        assert len(replies) == 1 and replies[0].body == "NATIVE_GOAL_WAIT_HANDLED"
        with Coordination(str(comms.root / "coordination.sqlite3")) as store:
            proofs = read_historical_native_inputs(
                store, wire_root_id=root_id,
                recipient_lookup=stable_thread_lookup(owner.created_at),
                source_seq=original.seq,
            )
            assert len(proofs) == 1 and isinstance(proofs[0].execution, FullNativeExecution)
            assert proofs[0].expected_prompt_equality_established
        comms.agents.finish_turn(peer_lease)
        assert await agent.inputs.drain_inbox(sid) == 0
        assert native.provider.posts == 3
        from agent_comms.comms import Comms

        reopened = Comms(comms.root, private_initial_writes=False, private_claim_writes=False)
        assert reopened.goals.goal_wait(sid) is None
        assert reopened.registry.require(sid).goal == parked_goal
        assert reopened.bus.log.message_by_id(original.message_id).reference == original.reference
        assert InputDispositions(comms.root / InputDispositions.filename).read().lookup(old_key) == old
    finally:
        gate.set()
        native.provider.response_gate = None
        await asyncio.gather(turn, return_exceptions=True)
