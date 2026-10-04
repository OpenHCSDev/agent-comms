"""Real saved native owner: Retry defers until the original response retires."""

import asyncio
import json
import os

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import BlockedGoalAction, GoalPrecondition, SetGoalAction
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.goal_generation import ReadyGeneration
from agent_comms.goals import Goal
from agent_comms.runtime import RuntimeProxy, socket_path
from test_backend_native_lifecycle import native_backend


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")
async def test_native_retry_waits_for_current_response_without_replaying_unknown(native_backend):
    native = native_backend
    await native.author_history()
    release, finish_goal = asyncio.Event(), asyncio.Event()
    native.provider.response_gate = release
    async with native.open_owner(runtime_enabled=True, auto_wake=True) as (owner, session):
        comms = owner._comms
        store = owner.turns.goals.open_goal_store()
        goal = comms.goals.update_goal(
            session, SetGoalAction(text="Finish native retry objective"), owner_store=store
        )
        reservation = store.reserve(goal.id, 1)
        store.claim_launch(reservation)
        reservation.fail(store, "Previous native goal attempt failed")
        blocked = comms.goals.update_goal(
            session,
            BlockedGoalAction(
                expect=GoalPrecondition(goal_id=goal.id),
                block_reason="Previous native goal attempt failed",
            ),
        )
        # This retained unconfirmed reservation is input evidence, never a
        # source of a runnable prompt. It carries no fabricated native start.
        owner.inputs.dispositions.record(
            "acp:old-native-unknown", seq=None, owner=session,
            admission=comms.registry.snapshot().admission_generations[session],
            target=session, text="Uncertain old native input must never replay",
        )
        original = owner.inputs.dispositions.read().lookup("acp:old-native-unknown")
        proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
        turn = asyncio.create_task(proxy.request(
            "prompt", prompt=[{"type": "text", "text": "Unrelated ordinary native request"}],
        ))
        try:
            async with asyncio.timeout(30):
                while native.provider.posts != 1:
                    assert not turn.done()
                    await asyncio.sleep(0.01)
            lease = comms.registry.require(session).require_turn_lease()
            assert owner.turns.owns_turn(session, lease.turn_id)
            assert session in owner.inputs.backend_inboxes
            resumed = await proxy.request(
                "retry_goal", goal_id=goal.id, expected_revision=blocked.revision,
            )
            resumed_goal = FieldCodec.decode(Goal, resumed["goal"])
            assert resumed_goal.state.active and resumed_goal.id == goal.id
            generation = GoalAttemptStore(store.root).snapshot(goal.id)
            assert (generation.number, generation.lifecycle, generation.attempt_id) == (
                2, ReadyGeneration(), None,
            )
            await owner.turns.goals.schedule_goal(session)
            assert native.provider.posts == 1 and not owner.inputs.pending_turns.get(session)
            # The original localhost response resource supplies both gates;
            # no alternate model catalog, HTTP server or native event script.
            native.provider.response_gate = finish_goal
            release.set()
            assert (await turn)["stopReason"] == "end_turn"
            async with asyncio.timeout(30):
                while native.provider.posts != 2:
                    await asyncio.sleep(0.01)
            next_lease = comms.registry.require(session).require_turn_lease()
            assert next_lease != lease and owner.turns.owns_turn(session, next_lease.turn_id)
            paused = await proxy.request("pause_goal")
            assert FieldCodec.decode(Goal, paused["goal"]).state.declared_name == "paused"
            finish_goal.set()
            await owner.inputs.wake_tasks[session]
            assert not owner.turns.turn_state(session).busy
            assert not owner.inputs.backend_inboxes
            assert native.provider.posts == 2 and len(native.saved_inputs()) == 3
            assert owner.inputs.dispositions.read().lookup(original.key) == original
            assert all(original.text not in json.dumps(request) for request in native.provider.requests)
        finally:
            release.set()
            finish_goal.set()
            await proxy.close()
            if not turn.done():
                turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
    assert all(not child.alive() and not child.platform.group_members(child.identity)
               for child in native.children)
