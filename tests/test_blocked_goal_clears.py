"""A backend failure blocks the goal; the owner's next verified turn clears it."""

import asyncio

import pytest

from agent_comms.nominal.field_codec import InputId
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_generation import BlockedGeneration, ReadyGeneration
from agent_comms.queued_input import InitialInput
from agent_comms.repo import Checkout
from agent_comms.turn_failure import BackendFailed, FailedTurnResolution
from delivery_owner_fixture import reply_turn_finished
from native_backend_fixture import native_backend_fixture


def block_current_goal(comms, worker):
    goal = comms.goals.update_goal(worker, SetGoalAction(text="Keep working"))
    store = comms.goals.open_goal_store()
    ready = store.require_generation(goal.id)
    store.reserve(goal.id, ready.number).fail(store, FailedTurnResolution(BackendFailed))
    assert store.require_generation(goal.id).lifecycle == BlockedGeneration()
    return store, goal


@pytest.mark.asyncio
async def test_verified_turn_clears_a_backend_failed_goal(tmp_path, native_runtime_package):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as saved:
        await saved.author_history()
        async with saved.open_owner(native_options=()) as (agent, session_id):
            comms = agent._comms
            worker = agent.sessions.require(session_id)
            store, goal = block_current_goal(comms, worker)

            async with asyncio.timeout(40):
                await InitialInput.run(agent.inputs, session_id, worker, "Next message",
                                       display_text="Next message", input_id=InputId.mint())

            assert store.require_generation(goal.id).lifecycle == ReadyGeneration()


@pytest.mark.asyncio
async def test_verified_bus_turn_clears_a_backend_failed_goal(tmp_path, native_runtime_package):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        await native.author_history()
        async with native.open_owner(runtime_enabled=True, auto_wake=True) as (agent, session):
            comms = agent._comms
            store, goal = block_current_goal(comms, agent.sessions.require(session))
            comms.threads.claim_thread('sender', tags=frozenset(), worktree=Checkout(native.project))
            native.provider.text = 'Verified bus answer.'
            comms.messaging.send('sender', session, 'Next bus message')

            async with asyncio.timeout(60):
                await agent.inputs.drain_inbox(session)
                await reply_turn_finished(agent, session)

            assert native.provider.posts == 1
            assert store.require_generation(goal.id).lifecycle == ReadyGeneration()
