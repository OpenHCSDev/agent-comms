"""Goal settlement reuses the real SQLite grants and nominal state hierarchy."""

import pytest

from agent_comms import agent_events as events
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_attempts import GoalAttemptError, GoalAttemptStore
from agent_comms.goal_generation import CompletedGeneration, ReadyGeneration, ReservedGeneration
from agent_comms.goal_states import ActiveGoal, CompletedGoal, PausedGoal
from agent_comms.goals import Goal
from agent_comms.thread_identity import TurnId
from agent_comms.turn_goal_account import OriginGoalSettlement, VerifiedGoalSettlement
from test_s1_event_behavior import owner_turn as owner_turn


def test_goal_settlement_and_new_state_subclass_use_same_durable_owner(tmp_path):
    class SpecializedPausedGoal(PausedGoal):
        pass

    store = GoalAttemptStore.initialize(tmp_path / "attempts")
    turn = TurnId("turn")
    for settlement in (VerifiedGoalSettlement, OriginGoalSettlement):
        for state in (ActiveGoal(), PausedGoal(), CompletedGoal(), SpecializedPausedGoal()):
            goal = Goal(
                "work",
                f"{settlement.__name__}-{state.declared_name}",
                reported_turn=turn.value,
                state=state,
            )
            store.create_goal(goal.id)
            grant = store.ready_grant(goal.id, 1)
            permit = store.claim_launch(store.reserve(goal.id, 1, ready_grant=grant))
            result = settlement(store, permit, goal, turn)
            result.dispatch_sync(state)
            assert result.recorded
            assert store.snapshot(goal.id).lifecycle == (
                CompletedGeneration() if state.terminal else ReadyGeneration()
            )
            assert goal.state is state  # Recording never resumes an owner-paused goal.


@pytest.mark.asyncio
async def test_failed_origin_claim_retains_unresolved_reservation_and_blocks_goal(
    owner_turn, monkeypatch
):
    execution, progress = owner_turn
    runner = execution.runner
    goal = runner.comms.goals.update_goal(execution.thread_name, SetGoalAction(text="new work"))
    store = runner.open_goal_store()

    def lost_claim(_reservation):
        raise GoalAttemptError("claim outcome unavailable")

    monkeypatch.setattr(store, "claim_launch", lost_claim)
    with pytest.raises(GoalAttemptError, match="claim outcome unavailable"):
        progress.goals.tool_ended(events.ToolEnd("goal", "comms_set_goal", True, "created"))
    progress.goals.finish(None)
    assert store.snapshot(goal.id).lifecycle == ReservedGeneration()
    assert (
        runner.comms.registry.require(execution.thread_name).goal.state.reason
        == "Goal origin turn did not finish successfully."
    )
    assert execution.thread_name not in runner.pending_goal_origins
