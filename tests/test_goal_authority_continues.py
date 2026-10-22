"""An order or retry that re-activates a goal keeps the running turn; pausing or another goal ends it."""

from agent_comms.goal_states import ActiveGoal, BlockedGoal, PausedGoal
from agent_comms.goals import GoalAuthority, NoGoal


def test_reactivated_goal_keeps_the_running_turn():
    captured = GoalAuthority("g", BlockedGoal("Backend turn ended without a result."))
    assert captured.keeps_turn_under(GoalAuthority("g", ActiveGoal()))


def test_paused_goal_ends_the_running_turn():
    assert not GoalAuthority("g", ActiveGoal()).keeps_turn_under(GoalAuthority("g", PausedGoal()))


def test_another_goal_ends_the_running_turn():
    assert not GoalAuthority("g", ActiveGoal()).keeps_turn_under(GoalAuthority("h", ActiveGoal()))
    assert not GoalAuthority("g", ActiveGoal()).keeps_turn_under(NoGoal())
    assert not NoGoal().keeps_turn_under(GoalAuthority("g", ActiveGoal()))
