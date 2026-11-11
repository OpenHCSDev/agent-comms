"""A replaced order keeps the running turn's authority, so following input joins that turn; a new original captures the current goal."""

import asyncio
import os
from contextlib import AsyncExitStack, ExitStack
from dataclasses import replace

import pytest

from agent_comms.execution.acp import CommsAgent
from agent_comms.execution.backend import TurnSession
from agent_comms.execution.channel_coding_tools import OrdinaryGoalRule
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.controller import NO_CONTROLLER
from agent_comms.execution.owned_send import OwnedSend
from agent_comms.execution.owned_turn import OwnedTurn
from agent_comms.execution.queued_input import InitialInput, QueuedInput
from agent_comms.execution.reservation_rules import ReservationViolationError
from agent_comms.execution.turn_inbox import InputContent
from agent_comms.execution.turn_input_source import NoTurnAcceptance
from agent_comms.nominal.field_codec import AcpSessionId, InputId, OrderId
from agent_comms.planning.goal_actions import (
    BlockedGoalAction,
    ClearGoalAction,
    CreateProblemGoalAction,
    OrderGoalAction,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.planning.goal_states import ActiveGoal
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.recorded_time import RecordedEventTime
from compaction_loopback import LoopbackProvider
from conftest import answer_rules, declare_team, installed_comms
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
        async with saved.open_owner(native_options=(), auto_wake=False) as (agent, session_id):
            comms = agent._comms
            worker = agent.sessions.require(session_id)
            declare_team(comms, NativeThread(
                'colleague', frozenset(), Checkout(saved.project),
                process_identity=ProcessIdentity.capture(os.getpid())))
            answer_rules(comms, worker)
            request = comms.goals.todos.add_request(
                comms.registry.require('colleague').incarnation, 'Repair following input').id
            comms.goals.update_goal(worker, CreateProblemGoalAction(
                problem='P1', order=order(source), request=request,
                review_at=RecordedEventTime(1000),
                assigner=comms.registry.require('colleague').incarnation,
                repo=RepositoryName.from_wire('org/repo'), text='Repair following input',
            ))
            saved.provider.response_gate = asyncio.Event()
            task = asyncio.create_task(InitialInput.run(
                agent.inputs, session_id, worker, 'Original input before replacement',
                display_text='Original input before replacement',
             input_id=InputId.mint()))
            try:
                await wait_for(lambda: saved.provider.posts == 1)
                sender = TurnSession.active[task].send_boundary
                assert isinstance(sender, OwnedSend)
                original_turn = sender.turn
                original_process = comms.registry.require(worker).process_identity
                todo = comms.goals.todos.get('P1')
                replaced = comms.goals.update_goal(worker, OrderGoalAction(
                    problem='P1', expected_revision=todo.revision,
                    assigner=comms.registry.require('colleague').incarnation, request=request, review_at=RecordedEventTime(2000),
                    order=replace(order(source), id=OrderId.mint()),
                ))
                # A replaced order keeps the running turn's authority; only following input waits.
                OrdinaryGoalRule.require(sender.goal_permission, replaced)
                async with QueuedInput.reserve(
                    agent.inputs, session_id, worker, text='Following input after replacement',
                    prompt='Following input after replacement', echo=True, images=(), controller=NO_CONTROLLER,
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
async def test_clearing_goal_keeps_an_accepted_native_inputs_read_tool(
    tmp_path, native_runtime_package,
):
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as saved:
        await saved.author_history()
        (saved.project / 'probe.txt').write_text('read after clearing the goal\n')
        async with saved.open_owner(native_options=(), auto_wake=False) as (agent, session_id):
            comms = agent._comms
            worker = agent.sessions.require(session_id)
            answer_rules(comms, worker)
            comms.goals.update_goal(worker, SetGoalAction(text='Finish the old assignment'))
            saved.provider.tool_call = ('read', {'path': 'probe.txt'})
            saved.provider.response_gate = asyncio.Event()
            task = asyncio.create_task(InitialInput.run(
                agent.inputs, session_id, worker, 'Read probe.txt for the next task',
                display_text='Read probe.txt for the next task', input_id=InputId.mint(),
            ))
            try:
                await wait_for(lambda: saved.provider.posts == 1)
                sender = TurnSession.active[task].send_boundary
                original_turn = sender.turn
                original_process = comms.registry.require(worker).process_identity
                comms.goals.update_goal(worker, ClearGoalAction(), actor=OwnerInvocable)
                saved.provider.response_gate.set()
                async with asyncio.timeout(40):
                    await task
                assert comms.registry.require(worker).turn_generation == 1
                assert comms.registry.require(worker).process_identity == original_process
                assert sender.turn == original_turn
                assert saved.provider.posts == 2  # original request, then successful tool reply
                messages = saved.session.read_text()
                assert 'read after clearing the goal' in messages
                assert 'captured goal permission no longer applies' not in messages
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


WORKER_SESSION = AcpSessionId('worker-session')


@pytest.fixture
async def prepared_owner(tmp_path):
    comms = installed_comms(tmp_path)
    for name in ('colleague', 'worker'):
        comms.registry.declare(NativeThread(
            name, frozenset(), Checkout(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid())))
    agent = CommsAgent(comms, auto_wake=False)
    try:
        await agent.sessions.bind_owned(comms.registry.require('worker'), WORKER_SESSION)
        yield comms, agent
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_goal_published_during_preparation_is_current_at_reservation(tmp_path, prepared_owner):
    source = SourceRepository.create(tmp_path / 'source')
    comms, agent = prepared_owner
    request = comms.goals.todos.add_request(
        comms.registry.require('colleague').incarnation, 'Repair the turn owner').id
    initial = comms.goals.update_goal('worker', CreateProblemGoalAction(
        problem='P1', order=order(source), request=request,
        review_at=RecordedEventTime(1000),
        assigner=comms.registry.require('colleague').incarnation,
        repo=RepositoryName.from_wire('org/repo'), text='Repair the turn owner',
    ))
    actor = comms.registry.require('colleague').incarnation
    old = OwnedTurn(agent.turns, WORKER_SESSION, 'worker', 'first input',
                    original_owner_input=True, original_goal_id=initial.id,
                    acceptance=NoTurnAcceptance())
    async with AsyncExitStack() as resources, AsyncExitStack() as permits:
        assert await old.acquire(resources, permits)
        OrdinaryGoalRule.require(old.original.goal_permission, initial)
        todo = comms.goals.todos.get('P1')
        replaced = comms.goals.update_goal('worker', OrderGoalAction(
            problem='P1', expected_revision=todo.revision, assigner=actor, request=request, review_at=RecordedEventTime(2000),
            order=replace(order(source), id=OrderId.mint()),
        ))
        OrdinaryGoalRule.require(old.original.goal_permission, replaced)

    # Exercise the real lease-to-reservation boundary without replacing any methods.
    # A second teammate publication during preparation makes the leased snapshot stale.
    current = OwnedTurn(agent.turns, WORKER_SESSION, 'worker', 'next input',
                        original_owner_input=True, original_goal_id=replaced.id,
                        acceptance=NoTurnAcceptance())
    assert current.authorize()
    async with AsyncExitStack() as resources:
        permits = ExitStack()
        resources.callback(permits.close)
        assert current.prepare(resources, permits)
        todo = comms.goals.todos.get('P1')
        latest = comms.goals.update_goal('worker', OrderGoalAction(
            problem='P1', expected_revision=todo.revision, assigner=actor, request=request, review_at=RecordedEventTime(3000),
            order=replace(order(source), id=OrderId.mint()),
        ))
        assert current.thread.goal == replaced != latest
        current.reserve_input(current.prepared)
        try:
            assert current.original.keys != old.original.keys
            # This is the same goal check CodingToolOwner.allow runs before a tool.
            OrdinaryGoalRule.require(current.original.goal_permission, comms.registry.require('worker').goal)
        finally:
            current.settle_unbound()


@pytest.mark.asyncio
async def test_order_reactivating_a_blocked_goal_keeps_the_running_turn(tmp_path, prepared_owner):
    """The retry order's message is queued while the goal is blocked, so its owner input
    carries no active goal id and captures the blocked state; the order then makes the goal
    active, and that running turn keeps its tool calls."""
    source = SourceRepository.create(tmp_path / 'source')
    comms, agent = prepared_owner
    request = comms.goals.todos.add_request(
        comms.registry.require('colleague').incarnation, 'Repair the blocked teammate').id
    comms.goals.update_goal('worker', CreateProblemGoalAction(
        problem='P1', order=order(source), request=request,
        review_at=RecordedEventTime(1000),
        assigner=comms.registry.require('colleague').incarnation,
        repo=RepositoryName.from_wire('org/repo'), text='Repair the blocked teammate',
    ))
    blocked = comms.goals.update_goal(
        'worker', BlockedGoalAction(block_reason='Backend turn ended without a result.')
    )
    actor = comms.registry.require('colleague').incarnation
    # QueuedInputContext.active_goal_id of a blocked goal, as queued_input.py passes it.
    turn = OwnedTurn(agent.turns, WORKER_SESSION, 'worker', 'retry order message',
                     original_owner_input=True,
                     original_goal_id=blocked.active().reported_id(), acceptance=NoTurnAcceptance())
    async with AsyncExitStack() as resources, AsyncExitStack() as permits:
        assert await turn.acquire(resources, permits)
        todo = comms.goals.todos.get('P1')
        retried = comms.goals.update_goal('worker', OrderGoalAction(
            problem='P1', expected_revision=todo.revision, assigner=actor, request=request, review_at=RecordedEventTime(2000),
            order=replace(order(source), id=OrderId.mint()),
        ))
        assert retried.state == ActiveGoal()
        # The same check CodingToolOwner.allow runs before each of the turn's tool calls.
        OrdinaryGoalRule.require(turn.original.goal_permission, comms.registry.require('worker').goal)


@pytest.mark.asyncio
async def test_an_owner_pause_holds_off_a_correction_of_the_goals_own_order(
    tmp_path, prepared_owner,
):
    """A correction asks no consent, but a pause is the goal's own state and refuses it."""
    source = SourceRepository.create(tmp_path / 'source')
    comms, _ = prepared_owner
    colleague = comms.registry.require('colleague').incarnation
    request = comms.goals.todos.add_request(colleague, 'Repair the paused teammate').id
    comms.goals.update_goal('worker', CreateProblemGoalAction(
        problem='P1', order=order(source), request=request,
        review_at=RecordedEventTime(1000), assigner=colleague,
        repo=RepositoryName.from_wire('org/repo'), text='Repair the paused teammate',
    ))
    paused = comms.goals.update_goal('worker', PausedGoalAction(), actor=OwnerInvocable)
    todo = comms.goals.todos.get('P1')
    with pytest.raises(ValueError) as refused:
        comms.goals.update_goal('worker', OrderGoalAction(
            problem='P1', expected_revision=todo.revision, assigner=colleague, request=request,
            review_at=RecordedEventTime(2000), order=replace(order(source), id=OrderId.mint()),
        ))
    assert str(refused.value) == paused.state.pause_source.instruction()
    assert comms.registry.require('worker').goal == paused
    assert comms.goals.todos.get('P1') == todo
