"""Goal settlement reuses the real SQLite grants and nominal state hierarchy."""

from agent_comms.execution.reported_turn import ReportedTurn
from agent_comms.execution.turn_goal_account import (
    OriginGoalSettlement,
    VerifiedGoalSettlement,
)
from agent_comms.nominal.field_codec import GoalId, TurnId
from agent_comms.nominal.lifecycle_capability import Terminal
from agent_comms.planning.goal_attempts import GoalAttemptStore
from agent_comms.planning.goal_generation import (
    CompletedGeneration,
    ReadyGeneration,
)
from agent_comms.planning.goal_states import (
    ActiveGoal,
    CompletedGoal,
    PausedGoal,
)
from agent_comms.planning.goals import Goal


def test_goal_settlement_and_new_state_subclass_use_same_durable_owner(tmp_path):
    class SpecializedPausedGoal(PausedGoal):
        pass

    (tmp_path / "attempts").mkdir(mode=0o700)
    store = GoalAttemptStore.initialize(tmp_path / "attempts")
    turn = TurnId('9b473845f684444f80a26586ae298d2b')
    for settlement in (VerifiedGoalSettlement, OriginGoalSettlement):
        for state in (ActiveGoal(), PausedGoal(), CompletedGoal(), SpecializedPausedGoal()):
            goal_id = GoalId.mint()
            goal = Goal(
                "work",
                goal_id,
                published_seq=0,
                reported_turn=ReportedTurn(turn),
                state=state,
            )
            store.create_goal(goal.id)
            grant = store.ready_grant(goal.id, 1)
            permit = store.claim_launch(store.reserve(goal.id, 1, ready_grant=grant))
            result = settlement(store, permit, goal, turn)
            result.dispatch_sync(state)
            assert permit.has_verified_progress(store)
            assert store.snapshot(goal.id).lifecycle == (
                CompletedGeneration() if bool(state.items_with(Terminal, True)) else ReadyGeneration()
            )
            assert goal.state is state  # Recording never resumes an owner-paused goal.


