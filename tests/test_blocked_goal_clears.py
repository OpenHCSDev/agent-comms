"""A backend failure blocks the goal; the owner's next verified turn clears it."""

import asyncio

import pytest

from agent_comms.field_codec import InputId
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_generation import BlockedGeneration, ReadyGeneration
from agent_comms.queued_input import InitialInput
from native_backend_fixture import native_backend_fixture


@pytest.mark.asyncio
async def test_verified_turn_clears_a_backend_failed_goal(tmp_path, native_runtime_package):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as saved:
        await saved.author_history()
        async with saved.open_owner(native_options=()) as (agent, session_id):
            comms = agent._comms
            worker = agent.sessions.require(session_id)
            goal = comms.goals.update_goal(worker, SetGoalAction(text="Keep working"))
            store = comms.goals.open_goal_store()
            ready = store.require_generation(goal.id)
            store.reserve(goal.id, ready.number).fail(store, "backend_failed")
            assert store.require_generation(goal.id).lifecycle == BlockedGeneration()

            async with asyncio.timeout(40):
                await InitialInput.run(agent.inputs, session_id, worker, "Next message",
                                       display_text="Next message", input_id=InputId.mint())

            assert store.require_generation(goal.id).lifecycle == ReadyGeneration()
