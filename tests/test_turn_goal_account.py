"""Goal settlement reuses the real SQLite grants and nominal state hierarchy."""

from agent_comms.lifecycle_capability import Terminal



from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.goal_generation import (
    CompletedGeneration, ReadyGeneration,
)
from agent_comms.goal_states import ActiveGoal, CompletedGoal, PausedGoal
from agent_comms.goals import Goal, ReportedTurn
from agent_comms.thread_identity import RegisteredTurn
from agent_comms.turn_goal_account import OriginGoalSettlement, VerifiedGoalSettlement



def test_goal_settlement_and_new_state_subclass_use_same_durable_owner(tmp_path):
    class SpecializedPausedGoal(PausedGoal):
        pass

    (tmp_path / "attempts").mkdir(mode=0o700)
    store = GoalAttemptStore.initialize(tmp_path / "attempts")
    turn = RegisteredTurn("turn")
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
                CompletedGeneration() if bool(state.items_with(Terminal, True)) else ReadyGeneration()
            )
            assert goal.state is state  # Recording never resumes an owner-paused goal.


