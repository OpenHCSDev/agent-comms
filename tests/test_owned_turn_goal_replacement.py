"""A replaced order keeps the running turn's authority, so following input joins that turn; a new original captures the current goal."""

import asyncio
import os
from contextlib import AsyncExitStack
from dataclasses import replace

import pytest

from agent_comms import backend
from agent_comms.acp import CommsAgent
from agent_comms.agent_backend import InputContent
from agent_comms.channel_input_batch import InputBatch
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import InputId, TurnId
from agent_comms.goal_actions import (
    BlockedGoalAction,
    CreateProblemGoalAction,
    OrderGoalAction,
)
from agent_comms.goal_states import ActiveGoal
from agent_comms.owned_send import OwnedSend
from agent_comms.owned_turn import OwnedTurn
from agent_comms.queued_input import InitialInput, QueuedInput
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.reservation_rules import ReservationViolationError
from agent_comms.threads import Thread
from agent_comms.turn_input_source import NoTurnAcceptance
from compaction_loopback import LoopbackProvider
from conftest import installed_comms
from native_backend_fixture import native_backend_fixture, wait_for
from source_revision_fixture import SourceRepository
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
        source = SourceRepository.create(tmp_path / 'source')
        (saved.project / 'probe.txt').write_text('following input reached the new turn\n')
        async with saved.open_owner(native_options=()) as (agent, session_id):
            comms = agent._comms
            worker = agent.sessions.require(session_id)
            comms.registry.declare(Thread(
                'lead', frozenset(), str(saved.project),
                process_identity=ProcessIdentity.capture(os.getpid()),
            ))
            request = comms.goals.todos.add_request(
                comms.registry.require('lead').incarnation, 'Repair following input').id
            comms.goals.update_goal(worker, CreateProblemGoalAction(
                problem='P1', order=order(source), request=request,
                review_at=RecordedEventTime(1000), commander='lead',
                assigner=comms.registry.require('lead').incarnation,
                repo=RepositoryName.from_wire('org/repo'), text='Repair following input',
            ))
            saved.provider.response_gate = asyncio.Event()
            task = asyncio.create_task(InitialInput.run(
                agent.inputs, session_id, worker, 'Original input before replacement',
                display_text='Original input before replacement',
             input_id=InputId.mint()))
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
                    commander='lead', request=request, review_at=RecordedEventTime(2000),
                    order=replace(order(source), id='replacement'),
                ))
                # A replaced order keeps the running turn's authority; only following input waits.
                sender.goal_permission.require_current(replaced)
                async with QueuedInput.reserve(
                    agent.inputs, session_id, worker, text='Following input after replacement',
                    prompt='Following input after replacement', echo=True, images=(), controller=None,
                 input_id=InputId.mint()) as reservation:
                    item = reservation.item
                    item.place(agent.inputs, session_id, agent.inputs.state(session_id).require_inbox(),
                               InputContent(text=item.prompt))
                    reservation.release()
                saved.provider.response_gate.set()
                async with asyncio.timeout(40):
                    await task
                started = agent.inputs.dispositions.lookup(item.key)
                assert started.turn_id == original_turn
                assert comms.registry.require(worker).turn_generation == 1
                assert comms.registry.require(worker).process_identity == original_process
                assert comms.registry.require(worker).goal == replaced
                assert not comms.registry.require(worker).executing
                assert not agent.inputs.state(session_id).queued
                assert saved.provider.posts == 3  # original, following input with read, tool reply
                messages = saved.session.read_text()
                assert 'following input reached the new turn' in messages
                assert 'captured goal permission no longer applies' not in messages
                with pytest.raises(ReservationViolationError, match='acknowledgement is missing'):
                    sender.source('unknown-public-input')
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_goal_published_during_preparation_is_current_at_reservation(tmp_path):
    source = SourceRepository.create(tmp_path / 'source')
    comms = installed_comms(tmp_path)
    for name in ('lead', 'worker'):
        comms.registry.declare(Thread(
            name, frozenset(), str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        ))
    request = comms.goals.todos.add_request(
        comms.registry.require('lead').incarnation, 'Repair the turn owner').id
    initial = comms.goals.update_goal('worker', CreateProblemGoalAction(
        problem='P1', order=order(source), request=request,
        review_at=RecordedEventTime(1000), commander='lead',
        assigner=comms.registry.require('lead').incarnation,
        repo=RepositoryName.from_wire('org/repo'), text='Repair the turn owner',
    ))
    actor = comms.registry.require('lead').incarnation
    agent = CommsAgent(comms, auto_wake=False)
    old = OwnedTurn(agent.turns, 'worker-session', 'worker', 'first input',
                    original_owner_input=True, original_goal_id=initial.id,
                    acceptance=NoTurnAcceptance())
    async with AsyncExitStack() as resources, AsyncExitStack() as permits:
        assert await old.acquire(resources, permits)
        old.original.goal_permission.require_current(initial)
        todo = comms.goals.todos.get('P1')
        replaced = comms.goals.update_goal('worker', OrderGoalAction(
            problem='P1', expected_revision=todo.revision, assigner=actor,
            commander='lead', request=request, review_at=RecordedEventTime(2000),
            order=replace(order(source), id='replacement'),
        ))
        old.original.goal_permission.require_current(replaced)

    # Exercise the real lease-to-reservation boundary without replacing any methods.
    # A second commander publication during preparation makes the leased snapshot stale.
    current = OwnedTurn(agent.turns, 'worker-session', 'worker', 'next input',
                        original_owner_input=True, original_goal_id=replaced.id,
                        acceptance=NoTurnAcceptance())
    assert current.authorize()
    current.turn_id = TurnId.mint()
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
            commander='lead', request=request, review_at=RecordedEventTime(3000),
            order=replace(order(source), id='replacement-during-preparation'),
        ))
        assert current.thread.goal == replaced != latest
        current.reserve_input(batch)
        try:
            assert current.original.keys != old.original.keys
            # This is the same goal check CodingToolOwner.allow runs before a tool.
            current.original.goal_permission.require_current(comms.registry.require('worker').goal)
        finally:
            current.settle_unbound()


@pytest.mark.asyncio
async def test_order_reactivating_a_blocked_goal_keeps_the_running_turn(tmp_path):
    """The retry order's message is queued while the goal is blocked, so its owner input
    carries no active goal id and captures the blocked state; the order then makes the goal
    active, and that running turn keeps its tool calls."""
    source = SourceRepository.create(tmp_path / 'source')
    comms = installed_comms(tmp_path)
    for name in ('lead', 'worker'):
        comms.registry.declare(Thread(
            name, frozenset(), str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        ))
    request = comms.goals.todos.add_request(
        comms.registry.require('lead').incarnation, 'Repair the blocked unit').id
    comms.goals.update_goal('worker', CreateProblemGoalAction(
        problem='P1', order=order(source), request=request,
        review_at=RecordedEventTime(1000), commander='lead',
        assigner=comms.registry.require('lead').incarnation,
        repo=RepositoryName.from_wire('org/repo'), text='Repair the blocked unit',
    ))
    blocked = comms.goals.update_goal(
        'worker', BlockedGoalAction(block_reason='Backend turn ended without a result.')
    )
    actor = comms.registry.require('lead').incarnation
    agent = CommsAgent(comms, auto_wake=False)
    # QueuedInputContext.active_goal_id of a blocked goal, as queued_input.py passes it.
    turn = OwnedTurn(agent.turns, 'worker-session', 'worker', 'retry order message',
                     original_owner_input=True,
                     original_goal_id=blocked.active().reported_id(), acceptance=NoTurnAcceptance())
    async with AsyncExitStack() as resources, AsyncExitStack() as permits:
        assert await turn.acquire(resources, permits)
        todo = comms.goals.todos.get('P1')
        retried = comms.goals.update_goal('worker', OrderGoalAction(
            problem='P1', expected_revision=todo.revision, assigner=actor,
            commander='lead', request=request, review_at=RecordedEventTime(2000),
            order=replace(order(source), id='retry'),
        ))
        assert retried.state == ActiveGoal()
        # The same check CodingToolOwner.allow runs before each of the turn's tool calls.
        turn.original.goal_permission.require_current(comms.registry.require('worker').goal)
