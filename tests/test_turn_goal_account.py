"""Goal settlement reuses the real SQLite grants and nominal state hierarchy."""

import asyncio
from threading import Event

import pytest

import agent_comms.agent_events as events
from agent_comms.acp import CommsAgent
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_attempts import GoalAttemptError, GoalAttemptStore
from agent_comms.goal_generation import (
    BlockedGeneration, CancelledGeneration, CompletedGeneration, ReadyGeneration,
)
from agent_comms.goal_states import ActiveGoal, CompletedGoal, PausedGoal
from agent_comms.goals import Goal, ReportedTurn
from agent_comms.thread_identity import TurnId
from agent_comms.turn_goal_account import OriginGoalSettlement, VerifiedGoalSettlement
from agent_comms.owned_turn import OwnedTurn
from agent_comms.input_attempt import NotSentInput
from agent_comms.turn_goal_account import TurnGoalAccount
from agent_comms.turn_input_source import OriginalTurnInput
from test_s1_event_behavior import owner_turn as owner_turn
from stored_inputs import stored_inputs

pytestmark = pytest.mark.usefixtures("fake_native_catalog")


@pytest.fixture
def comms(comms):
    """Turn acquisition launches an owner on the declared private protocol."""
    comms.messaging.initialize_private_initial_protocol()
    return comms


def test_goal_settlement_and_new_state_subclass_use_same_durable_owner(tmp_path):
    class SpecializedPausedGoal(PausedGoal):
        pass

    (tmp_path / "attempts").mkdir(mode=0o700)
    store = GoalAttemptStore.initialize(tmp_path / "attempts")
    turn = TurnId("turn")
    for settlement in (VerifiedGoalSettlement, OriginGoalSettlement):
        for state in (ActiveGoal(), PausedGoal(), CompletedGoal(), SpecializedPausedGoal()):
            goal = Goal(
                "work",
                f"{settlement.__name__}-{state.declared_name}",
                published_seq=0,
                reported_turn=ReportedTurn(turn.value),
                state=state,
            )
            store.create_goal(goal.id)
            grant = store.ready_grant(goal.id, 1)
            permit = store.claim_launch(store.reserve(goal.id, 1, ready_grant=grant))
            result = settlement(store, permit, goal, turn)
            result.dispatch_sync(state)
            assert permit.has_verified_progress(store)
            assert store.snapshot(goal.id).lifecycle == (
                CompletedGeneration() if state.terminal else ReadyGeneration()
            )
            assert goal.state is state  # Recording never resumes an owner-paused goal.


@pytest.mark.asyncio
async def test_failed_origin_claim_retires_unlaunched_origin_and_blocks_goal(
    owner_turn, monkeypatch
):
    execution, progress = owner_turn
    runner = execution.runner
    goal = runner.comms.goals.update_goal(execution.thread_name, SetGoalAction(text="new work"))
    store = runner.comms.goals.open_goal_store()

    def lost_claim(_reservation):
        raise GoalAttemptError("claim outcome unavailable")

    monkeypatch.setattr(store, "claim_launch", lost_claim)
    with pytest.raises(GoalAttemptError, match="claim outcome unavailable"):
        progress.goals.tool_ended(events.ToolEnd("goal", "comms_set_goal", True, "created"))
    progress.goals.finish(None)
    assert store.snapshot(goal.id).lifecycle == CancelledGeneration()
    assert (
        runner.comms.registry.require(execution.thread_name).goal.state.reason
        == "Goal origin turn did not finish successfully."
    )
    assert execution.thread_name not in runner.goals.pending_goal_origins


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure, expected_state",
    [
        pytest.param(failure, state, id=failure)
        for failure, state in (
            ("begin", ReadyGeneration()),
            ("prepare", BlockedGeneration()),
            ("reserve", BlockedGeneration()),
            ("receipt_capture", BlockedGeneration()),
            ("goal_retirement", BlockedGeneration()),
        )
    ],
)
async def test_acquired_claim_and_lease_retire_on_each_pre_native_failure(
    comms, tmp_path, monkeypatch, failure, expected_state
):
    """Keep unstarted goals ready; retire only resources already acquired."""
    owner = CommsAgent(
        comms, agent_bin="unused",
    )
    session = await owner.new_session(cwd=str(tmp_path), mcp_servers=[])
    name = owner.sessions.bindings[session.session_id]
    goal = await owner.turns.goals.set_goal(session.session_id, "preserve failed original")
    execution = OwnedTurn(owner.turns, session.session_id, name, "work", autonomous_goal=True)
    store = comms.goals.open_goal_store()
    generation_before = store.snapshot(goal.id)
    pending_before = tuple(owner.inputs.state(session.session_id).pending_turns)

    def fail(*args, **kwargs):
        raise RuntimeError("injected acquired resource failure")

    targets = {
        "begin": (comms.agents, "begin_turn"),
        "prepare": (OwnedTurn, "prepare_prompt"),
        "reserve": (OriginalTurnInput, "reserve"),
        "receipt_capture": (OriginalTurnInput, "reserve"),
        "goal_retirement": (TurnGoalAccount, "finish"),
    }
    target, method = targets[failure]
    if failure == "receipt_capture":
        reserve = target.reserve

        def fail(*args, **kwargs):
            original = reserve(*args, **kwargs)
            assert original.batch.originals
            raise RuntimeError("injected acquired resource failure")

    monkeypatch.setattr(target, method, fail)
    if failure == "goal_retirement":
        async def native_preparation_failure():
            raise ValueError("private preparation failure")
        monkeypatch.setattr(execution, "prepare_native", native_preparation_failure)
    try:
        with pytest.raises(RuntimeError, match="injected acquired resource failure"):
            await execution.run()
        assert store.snapshot(goal.id).lifecycle == expected_state
        assert not comms.registry.require(name).executing
        assert not owner.inputs.state(session.session_id).turn_open()
        assert not owner.inputs.state(session.session_id).turn_started()
        assert not owner.inputs.input_keys(session.session_id)
        assert tuple(owner.inputs.state(session.session_id).pending_turns) == pending_before
        if failure == "receipt_capture":
            originals = tuple(stored_inputs(owner.inputs.dispositions.root).rows.values())
            assert len(originals) == 1
            assert isinstance(originals[0], NotSentInput)
        if expected_state.ready:
            assert store.snapshot(goal.id) == generation_before
        else:
            with pytest.raises(GoalAttemptError):
                store.ready_grant(goal.id, store.snapshot(goal.id).number)
    finally:
        await owner.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_fails", [False, True])
async def test_cancelled_acquisition_joins_reserved_input_before_rollback(
    comms, tmp_path, monkeypatch, worker_fails,
):
    """Cancellation cannot leave a recorded input after suppressing worker delivery."""
    owner = CommsAgent(
        comms, agent_bin="unused", auto_wake=False,
    )
    session = await owner.new_session(cwd=str(tmp_path), mcp_servers=[])
    name = owner.sessions.bindings[session.session_id]
    execution = OwnedTurn(owner.turns, session.session_id, name, "cancel before native",
                          original_owner_input=True)
    captured, release = Event(), Event()
    reserve = OriginalTurnInput.reserve

    def paused_reservation(*args, **kwargs):
        original = reserve(*args, **kwargs)
        captured.set()
        assert release.wait(5), "Original reservation worker was not released"
        if worker_fails:
            raise RuntimeError("Original reservation worker failed after cancellation")
        return original

    monkeypatch.setattr(OriginalTurnInput, "reserve", paused_reservation)
    running = asyncio.create_task(execution.run())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(captured.wait, 5), 6)
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done(), "Cancellation escaped the original reservation worker"
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(running, 6)
        if worker_fails:
            assert isinstance(cancelled.value.__cause__, RuntimeError)
            assert str(cancelled.value.__cause__) == (
                "Original reservation worker failed after cancellation"
            )
        original, = stored_inputs(owner.inputs.dispositions.root).rows.values()
        assert isinstance(original, NotSentInput)
        assert comms.registry.require(name).turn_lease is None
        assert not owner.inputs.state(session.session_id).turn_started()
        assert not owner.inputs.state(session.session_id).turn_open()
        assert not owner.turns.persistent_backends
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await owner.shutdown()
