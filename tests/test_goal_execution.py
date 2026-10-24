"""Real turn acquisition and terminal handling share the execution owner."""

from agent_comms.turn_input_source import NoTurnAcceptance

import os
import sqlite3
from contextlib import AsyncExitStack, asynccontextmanager, closing
from dataclasses import fields

import pytest

from agent_comms import agent_events as events
from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    PausedGoalAction,
    RetryGoalAction,
    SetGoalAction,
)
from agent_comms.goal_failure_observation import FailedTurnEvidence
from agent_comms.goal_presentation import (
    AttemptFailedExecution,
    GoalExecution,
    GoalSnapshot,
    RunnableExecution,
    RunningExecution,
)
from agent_comms.goal_states import ActiveGoal
from agent_comms.owned_turn import OwnedTurn
from agent_comms.turn_terminal import OwnerStop
from agent_comms.threads import Thread


@pytest.fixture
def execution_bus(copied_goal_bus):
    comms = copied_goal_bus
    comms.registry.declare(Thread(
        "worker", frozenset(), str(comms.root.parent),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    comms.goals.update_goal("worker", SetGoalAction(text="Repair original work"))
    return comms


@pytest.mark.parametrize("execution_type", tuple(
    member for member in GoalExecution.members_with(GoalExecution)
    if fields(member) == fields(RunnableExecution)
))
def test_execution_identity_survives_wire_and_change_detection(execution_bus, execution_type):
    goal = execution_bus.registry.require("worker").goal
    execution = execution_type(goal.id)
    decoded = FieldCodec.decode(GoalExecution, FieldCodec.encode(execution))
    assert decoded == execution
    assert decoded.runnable() == execution.runnable()
    current = GoalSnapshot(goal, RunnableExecution(goal.id))
    assert current.execution.runnable()
    assert current.changed_by(GoalSnapshot(goal, execution)) == (current.execution != execution)


@asynccontextmanager
async def turn_on(comms, *, original_owner_input=False):
    agent = CommsAgent(comms, auto_wake=False, owner_stop=OwnerStop())
    owner = comms.registry.require("worker")
    session = "goal-execution-session"
    await agent.sessions.bind_owned(owner, session)
    turn = OwnedTurn(
        agent.turns, session, owner.name, "Continue original work",
        original_owner_input=original_owner_input, original_goal_id=owner.goal.require().id,
        acceptance=NoTurnAcceptance(),
    )
    async with AsyncExitStack() as resources, AsyncExitStack() as permits:
        assert await turn.acquire(resources, permits)
        yield turn


def failure_count(comms):
    with closing(sqlite3.connect(comms.goals.open_goal_store().path)) as db:
        return FailedTurnEvidence.count(db)


@pytest.mark.asyncio
async def test_failed_turn_writes_ledger_once_and_reads_failed_execution(execution_bus):
    comms = execution_bus
    goal = comms.registry.require("worker").goal.require()
    async with turn_on(comms) as turn:
        await turn.progress.done(events.Done("backend failed", False))
        turn.finish_goals()
        turn.finish_goals()
        assert failure_count(comms) == 1
        assert comms.registry.require("worker").goal == goal
        assert isinstance(comms.goals.goal_execution("worker"), RunningExecution)
    execution = comms.goals.goal_execution("worker")
    assert isinstance(execution, AttemptFailedExecution)
    assert execution.toggle is RetryGoalAction
    assert execution.evidence.turn_id == turn.turn_id
    assert failure_count(comms) == 1
    assert comms.registry.require("worker").goal.state == ActiveGoal()


@pytest.mark.asyncio
async def test_provider_retry_during_live_turn_writes_no_goal_failure(execution_bus):
    comms = execution_bus
    goal = comms.registry.require("worker").goal.require()
    async with turn_on(comms) as turn:
        store = comms.goals.open_goal_store()
        before = store.snapshot(goal.id)
        await turn.progress.done(events.Done("provider is retrying", False))
        await turn.progress.tool_ended(events.ToolEnd("read", "read", True, "source read"))
        assert store.snapshot(goal.id) == before
        assert failure_count(comms) == 0
        assert comms.registry.require("worker").goal == goal
        execution = comms.goals.goal_execution("worker")
        assert isinstance(execution, RunningExecution)
        assert execution.toggle is PausedGoalAction


@pytest.mark.asyncio
async def test_owner_stop_beats_backend_failure_and_next_turn_runs_without_retry(execution_bus):
    comms = execution_bus
    async with turn_on(comms) as turn:
        await turn.progress.done(events.Done("dying backend", False))
        turn.runner.owner_stop.request()
    # A new reader observes the same retained bus, without a Retry or goal write.
    relaunched = wire(comms.root)
    assert isinstance(relaunched.goals.goal_execution("worker"), RunnableExecution)
    assert relaunched.registry.require("worker").goal.state == ActiveGoal()
    assert failure_count(relaunched) == 0
    async with turn_on(relaunched) as next_turn:
        assert isinstance(relaunched.goals.goal_execution("worker"), RunningExecution)
        await next_turn.progress.tool_ended(events.ToolEnd("read", "read", True, "source read"))
        await next_turn.progress.done(events.Done("Continued without Retry", True))
    assert isinstance(relaunched.goals.goal_execution("worker"), RunnableExecution)
    assert failure_count(relaunched) == 0


@pytest.mark.asyncio
async def test_verified_productive_turn_recovers_blocked_generation(execution_bus):
    comms = execution_bus
    async with turn_on(comms) as failed:
        await failed.progress.done(events.Done("backend failed", False))
    assert isinstance(comms.goals.goal_execution("worker"), AttemptFailedExecution)
    async with turn_on(comms, original_owner_input=True) as productive:
        await productive.progress.tool_ended(events.ToolEnd("read", "read", True, "source read"))
        await productive.progress.done(events.Done("Verified productive progress", True))
    assert isinstance(comms.goals.goal_execution("worker"), RunnableExecution)
    assert comms.registry.require("worker").goal.state == ActiveGoal()
    assert failure_count(comms) == 1  # The old failure remains evidence, never replayed.
