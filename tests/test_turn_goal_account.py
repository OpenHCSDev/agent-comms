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


