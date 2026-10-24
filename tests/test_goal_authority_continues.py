"""An order or retry that re-activates a goal keeps the running turn; pausing or another goal ends it."""

from agent_comms.field_codec import GoalId
from agent_comms.goal_states import ActiveGoal, BlockedGoal, PausedGoal
from agent_comms.goals import GoalAuthority, NoGoal


def test_reactivated_goal_keeps_the_running_turn():
    g_goal_id = GoalId.mint()
    captured = GoalAuthority(g_goal_id, BlockedGoal("Backend turn ended without a result."))
    assert captured.keeps_turn_under(GoalAuthority(g_goal_id, ActiveGoal()))


def test_paused_goal_ends_the_running_turn():
    g_goal_id = GoalId.mint()
    assert not GoalAuthority(g_goal_id, ActiveGoal()).keeps_turn_under(GoalAuthority(g_goal_id,
        PausedGoal()))


def test_another_goal_ends_the_running_turn():
    g_goal_id = GoalId.mint()
    h_goal_id = GoalId.mint()
    assert not GoalAuthority(g_goal_id, ActiveGoal()).keeps_turn_under(GoalAuthority(h_goal_id,
        ActiveGoal()))
    assert not GoalAuthority(g_goal_id, ActiveGoal()).keeps_turn_under(NoGoal())
    assert not NoGoal().keeps_turn_under(GoalAuthority(g_goal_id, ActiveGoal()))
