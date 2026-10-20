"""Actual saved SDK/OwnedTurn settlement, with only localhost HTTP controlled."""

import asyncio

import pytest

from agent_comms.coordinator import Coordination
from agent_comms.goal_actions import ActiveGoalAction, GoalPrecondition, OwnerInvocable, SetGoalAction
from agent_comms.goal_generation import ReadyGeneration


@pytest.mark.parametrize("replace_goal", [False, True])
async def test_native_settlement_selects_only_original_goal(native_backend, replace_goal):
    native = native_backend
    await native.author_history()
    history = native.session.read_bytes()
    async with native.open_owner(runtime_enabled=True, auto_wake=False) as (agent, session):
        comms = agent._comms
        store = comms.goals.open_goal_store()
        original = await Coordination.run_worker(lambda: comms.goals.update_goal(
            session, SetGoalAction(text="One genuine native goal turn"),
            actor=OwnerInvocable,
        ))
        changed = []

        class ChangeAtOriginalProviderRequest:
            async def wait(self):
                current = await Coordination.run_worker(comms.registry.snapshot)
                assert current.require(session).turn_record.executing
                assert not changed
                changed.append(await Coordination.run_worker(lambda: comms.goals.update_goal(
                    session,
                    SetGoalAction(text="Independent replacement") if replace_goal
                    else ActiveGoalAction(
                        expect=GoalPrecondition(goal_id=original.id),
                        progress="Later progress on the original goal",
                    ),
                )))
                return True

        native.provider.response_gate = ChangeAtOriginalProviderRequest()
        try:
            async with asyncio.timeout(30):
                await agent.turns.run_agent_turn(
                    session, session, "One distinct original goal input", autonomous_goal=True,
                )
        finally:
            native.provider.response_gate = None
        assert len(changed) == 1
        current = comms.registry.require(session)
        assert current.goal == changed[0]
        assert not current.turn_record.executing
        assert native.provider.posts == 1
        assert len(native.saved_inputs()) == 2  # Original SDK seed + one actual native input.
        assert native.session.read_bytes().startswith(history)
        original_attempt = store.snapshot(original.id)
        if replace_goal:
            assert current.goal.id != original.id
            assert original_attempt.lifecycle.terminal
        else:
            assert current.goal.id == original.id and current.goal.revision > original.revision
            assert original_attempt.number == 2
            assert original_attempt.lifecycle == ReadyGeneration()
        assert not agent.turns.turn_tasks
        assert not any(state.turn_open() for state in agent.inputs.drained.values())
    assert all(child.retired for child in native.children)
