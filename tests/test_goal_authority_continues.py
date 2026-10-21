"""An order or retry that re-activates a goal keeps the running turn; pausing or another goal ends it."""

from agent_comms.goal_states import ActiveGoal, BlockedGoal, PausedGoal
from agent_comms.goals import GoalAuthority, NoGoal


def test_reactivated_goal_keeps_the_running_turn():
    captured = GoalAuthority("g", BlockedGoal("Backend turn ended without a result."))
    assert captured.admits(GoalAuthority("g", ActiveGoal()))


def test_paused_goal_ends_the_running_turn():
    assert not GoalAuthority("g", ActiveGoal()).admits(GoalAuthority("g", PausedGoal()))


def test_another_goal_ends_the_running_turn():
    assert not GoalAuthority("g", ActiveGoal()).admits(GoalAuthority("h", ActiveGoal()))
    assert not GoalAuthority("g", ActiveGoal()).admits(NoGoal())
    assert not NoGoal().admits(GoalAuthority("g", ActiveGoal()))
