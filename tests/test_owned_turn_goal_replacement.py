"""A replacement defers live following input to a newly leased native turn."""

import asyncio
import os
from dataclasses import replace

import pytest

from agent_comms import backend
from agent_comms.agent_backend import InputContent
from agent_comms.child_process import ProcessIdentity
from agent_comms.goal_actions import CreateProblemGoalAction, OrderGoalAction, SetGoalAction
from agent_comms.goals import RequestGoalOrder
from agent_comms.native_tool_call import SelectedToolDenied
from agent_comms.owned_send import OwnedSend
from agent_comms.queued_input import InitialInput, QueuedInput
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from compaction_loopback import LoopbackProvider
from native_backend_fixture import native_backend_fixture, wait_for
from test_todos import order


class FollowingReadProvider(LoopbackProvider):
    """The second actual model request exercises the following input's read grant."""

    def response_chunks(self):
        if self.posts == 2:
            self.tool_call = ('read', {'path': 'probe.txt'})
        yield from super().response_chunks()


@pytest.mark.asyncio
async def test_turn_after_order_replacement_is_allowed(tmp_path, native_runtime_package):
    async with native_backend_fixture(
        tmp_path, package=native_runtime_package, provider_type=FollowingReadProvider,
    ) as saved:
        await saved.author_history()
        (saved.project / 'probe.txt').write_text('following input reached the new turn\n')
        async with saved.open_owner(native_options=()) as (agent, session_id):
            comms = agent._comms
            worker = agent.sessions.require(session_id)
            comms.registry.declare(comms.registry.new_thread(
                'lead', frozenset(), str(saved.project),
                process_identity=ProcessIdentity.capture(os.getpid()),
            ))
            comms.goals.update_goal('lead', SetGoalAction(
                text='Repair following input', goal_id='request', order=RequestGoalOrder(),
            ))
            comms.goals.update_goal(worker, CreateProblemGoalAction(
                problem='P1', order=order(saved.project), request='request',
                deadline=RecordedEventTime(1000), commander='lead',
                repo=RepositoryName.from_wire('org/repo'), text='Repair following input',
            ))
            saved.provider.response_gate = asyncio.Event()
            task = asyncio.create_task(InitialInput.run(
                agent.inputs, session_id, worker, 'Original input before replacement',
                display_text='Original input before replacement',
            ))
            try:
                await wait_for(lambda: saved.provider.posts == 1)
                sender = backend.TurnSession.active[task].send_boundary
                assert isinstance(sender, OwnedSend)
                original_turn = sender.turn.value
                original_process = comms.registry.require(worker).process_identity
                todo = comms.goals.todos.get('P1')
                replaced = comms.goals.update_goal(worker, OrderGoalAction(
                    problem='P1', expected_revision=todo.revision,
                    assigner=comms.registry.require('lead').incarnation,
                    commander='lead', request='request', deadline=RecordedEventTime(2000),
                    order=replace(order(saved.project), id='replacement'),
                ))
                with pytest.raises(SelectedToolDenied, match='captured goal permission no longer applies'):
                    sender.goal_permission.require_current(replaced)
                async with QueuedInput.reserve(
                    agent.inputs, session_id, worker, text='Following input after replacement',
                    prompt='Following input after replacement', echo=True, images=(), controller=None,
                ) as reservation:
                    item = reservation.item
                    item.place(agent.inputs, session_id, agent.inputs.state(session_id).require_inbox(),
                               InputContent(text=item.prompt))
                    reservation.release()
                saved.provider.response_gate.set()
                async with asyncio.timeout(40):
                    await task
                started = agent.inputs.dispositions.lookup(item.key)
                assert started.turn_id != original_turn
                assert comms.registry.require(worker).turn_generation == 2
                assert comms.registry.require(worker).process_identity == original_process
                assert comms.registry.require(worker).goal == replaced
                assert not comms.registry.require(worker).executing
                assert not agent.inputs.state(session_id).queued
                assert saved.provider.posts == 3  # original, new original with read, tool reply
                messages = saved.session.read_text()
                assert 'following input reached the new turn' in messages
                assert 'captured goal permission no longer applies' not in messages
                with pytest.raises(KeyError):
                    sender.source('unknown-public-input')
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
