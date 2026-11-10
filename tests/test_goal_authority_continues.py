"""An order or retry that re-activates a goal keeps the running turn; pausing or another goal ends it."""

import pytest

from agent_comms.nominal.field_codec import GoalId
from agent_comms.planning.goal_authority import GoalAuthority
from agent_comms.planning.goal_states import (
    ActiveGoal,
    BlockedGoal,
    CompletedGoal,
    PausedGoal,
)
from agent_comms.planning.goals import NoGoal

RUNNING, OTHER = GoalId.mint(), GoalId.mint()


@pytest.mark.parametrize(("captured", "current", "keeps"), [
    pytest.param(GoalAuthority(RUNNING, BlockedGoal("Backend turn ended without a result.")),
                 GoalAuthority(RUNNING, ActiveGoal()), True, id="reactivated"),
    pytest.param(GoalAuthority(RUNNING, ActiveGoal()), GoalAuthority(RUNNING, PausedGoal()),
                 False, id="paused"),
    pytest.param(GoalAuthority(RUNNING, ActiveGoal()), GoalAuthority(OTHER, ActiveGoal()),
                 False, id="another-goal"),
    pytest.param(GoalAuthority(RUNNING, ActiveGoal()), NoGoal().authority, True, id="cleared"),
    pytest.param(GoalAuthority(RUNNING, CompletedGoal()), NoGoal().authority, True,
                 id="completed-then-cleared"),
    pytest.param(NoGoal().authority, GoalAuthority(RUNNING, ActiveGoal()), False, id="from-none"),
])
def test_running_turn_continues_only_under_its_reactivated_goal(captured, current, keeps):
    assert captured.keeps_turn_under(current) is keeps
