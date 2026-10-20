"""A new original captures the registry goal, not the lease's older snapshot."""

import os
from contextlib import AsyncExitStack
from dataclasses import replace

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.channel_input_batch import InputBatch
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.field_codec import Id128Text
from agent_comms.goal_actions import CreateProblemGoalAction, OrderGoalAction, SetGoalAction
from agent_comms.goals import RequestGoalOrder
from agent_comms.native_tool_call import SelectedToolDenied
from agent_comms.owned_turn import OwnedTurn
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from test_todos import order


@pytest.mark.asyncio
async def test_turn_after_order_replacement_is_allowed(tmp_path):
    comms = Comms(tmp_path)
    for name in ('lead', 'worker'):
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset(), str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        ))
    comms.goals.update_goal('lead', SetGoalAction(
        text='Repair the turn owner', goal_id='request', order=RequestGoalOrder(),
    ))
    initial = comms.goals.update_goal('worker', CreateProblemGoalAction(
        problem='P1', order=order(tmp_path), request='request',
        deadline=RecordedEventTime(1000), commander='lead',
        repo=RepositoryName.from_wire('org/repo'), text='Repair the turn owner',
    ))
    actor = comms.registry.require('lead').incarnation
    agent = CommsAgent(comms, auto_wake=False)
    old = OwnedTurn(agent.turns, 'worker-session', 'worker', 'first input',
                    original_owner_input=True, original_goal_id=initial.id)
    async with AsyncExitStack() as resources, AsyncExitStack() as permits:
        assert await old.acquire(resources, permits)
        old.original.goal_permission.require_current(initial)
        todo = comms.goals.todos.get('P1')
        replaced = comms.goals.update_goal('worker', OrderGoalAction(
            problem='P1', expected_revision=todo.revision, assigner=actor,
            commander='lead', request='request', deadline=RecordedEventTime(2000),
            order=replace(order(tmp_path), id='replacement'),
        ))
        with pytest.raises(SelectedToolDenied, match='captured goal permission no longer applies'):
            old.original.goal_permission.require_current(replaced)

    # Exercise the real lease-to-reservation boundary without replacing any methods.
    # A second commander publication during preparation makes the leased snapshot stale.
    current = OwnedTurn(agent.turns, 'worker-session', 'worker', 'next input',
                        original_owner_input=True, original_goal_id=replaced.id)
    assert current.authorize()
    current.turn_id = Id128Text.mint()
    async with AsyncExitStack() as resources:
        current.registry_owner = agent.turns.acquire_turn(
            resources, current.session_id, current.thread_name, current.turn_id, current.task,
        )
        batch = InputBatch.capture((), (), current.task, current.thread,
                                   agent.turns.inputs.dispositions.read(()))
        current.prepare_prompt(batch)
        todo = comms.goals.todos.get('P1')
        latest = comms.goals.update_goal('worker', OrderGoalAction(
            problem='P1', expected_revision=todo.revision, assigner=actor,
            commander='lead', request='request', deadline=RecordedEventTime(3000),
            order=replace(order(tmp_path), id='replacement-during-preparation'),
        ))
        assert current.thread.goal == replaced != latest
        current.reserve_input(batch)
        try:
            assert current.original.keys != old.original.keys
            # This is the same goal check CodingToolOwner.allow runs before a tool.
            current.original.goal_permission.require_current(comms.registry.require('worker').goal)
        finally:
            current.settle_unbound()
