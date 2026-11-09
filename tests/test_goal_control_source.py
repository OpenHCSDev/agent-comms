"""Goal controls preserve the caller's original authority and observed checkpoint."""

import pytest

from agent_comms.planning.goal_actions import ActiveGoalAction
from agent_comms.planning.goal_actions import ClearGoalAction
from agent_comms.planning.goal_actions import CompletedGoalAction
from agent_comms.planning.goal_actions import GoalAction
from agent_comms.planning.goal_actions import PausedGoalAction
from agent_comms.planning.goal_actions import RetryGoalAction
from agent_comms.planning.goal_actions import SetGoalAction
from agent_comms.planning.goal_control import GoalControlSource
from agent_comms.planning.goals import Goal
from agent_comms.planning.goals import GoalDeclaration
from agent_comms.planning.goals import NoGoal
from agent_comms.nominal.field_codec import GoalId


class ControlProbe(GoalControlSource[object, GoalAction[GoalDeclaration], Goal, GoalDeclaration]):
    def __init__(self, observed: GoalDeclaration, reply: GoalDeclaration) -> None:
        self.observed = observed
        self.reply = reply
        self.calls: list[tuple[object, ...]] = []

    async def observed_goal(self, authority: object) -> GoalDeclaration:
        self.calls.append((GoalControlSource.observed_goal, authority))
        return self.observed

    async def change_goal_control(
        self, authority: object, action: type[GoalAction[GoalDeclaration]], goal: Goal,
    ) -> GoalDeclaration:
        self.calls.append((GoalControlSource.change_goal_control, authority, action, goal))
        return self.reply

    async def set_goal_control(self, authority: object, text: str) -> GoalDeclaration:
        self.calls.append((GoalControlSource.set_goal_control, authority, text))
        return self.reply

    async def retry_goal_control(self, authority: object, goal: Goal) -> GoalDeclaration:
        self.calls.append((GoalControlSource.retry_goal_control, authority, goal))
        return self.reply


@pytest.mark.asyncio
@pytest.mark.parametrize("action", (ActiveGoalAction, PausedGoalAction, CompletedGoalAction, ClearGoalAction))
async def test_control_keeps_the_original_authority_and_checkpoint(
    action: type[GoalAction[GoalDeclaration]],
) -> None:
    original = Goal("Original objective", GoalId.mint(), 1, revision=7)
    reply = NoGoal()
    source = ControlProbe(original, reply)
    authority = object()
    assert await action.submit_control(source, authority) is reply
    assert source.calls == [
        (GoalControlSource.observed_goal, authority),
        (GoalControlSource.change_goal_control, authority, action, original),
    ]


@pytest.mark.asyncio
async def test_set_does_not_require_an_existing_goal() -> None:
    goal = Goal("New objective", GoalId.mint(), 1)
    source = ControlProbe(NoGoal(), goal)
    authority = object()
    assert await SetGoalAction.submit_control(source, authority, goal.text) is goal
    assert source.calls == [(GoalControlSource.set_goal_control, authority, goal.text)]


@pytest.mark.asyncio
async def test_retry_uses_the_original_goal_without_becoming_a_status_change() -> None:
    goal = Goal("Original objective", GoalId.mint(), 1, revision=7)
    source = ControlProbe(goal, goal)
    authority = object()
    assert await RetryGoalAction.submit_control(source, authority) is goal
    assert source.calls == [
        (GoalControlSource.observed_goal, authority),
        (GoalControlSource.retry_goal_control, authority, goal),
    ]
