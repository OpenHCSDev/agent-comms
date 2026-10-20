"""Owner intent survives failed execution; private attempt fences still fail closed."""

import json

import pytest

import agent_comms.agent_events as ae
from agent_comms.acp_failure import NativeTurnError
from agent_comms.goal_actions import (
    ActiveGoalAction,
    GoalPrecondition,
    ModelInvocable,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.goal_attempts import UnresolvedAttemptError
from agent_comms.goal_generation import BlockedGeneration
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent
from test_acp import TestAgentTurn as GoalFixture
from stored_inputs import stored_inputs


@pytest.mark.parametrize("attribution", ["model", "runtime"])
def test_nonowner_pause_does_not_bypass_failure_block(wired, tmp_path, attribution):
    wired.registry.declare(wired.registry.new_thread(name='project', tags=frozenset(), worktree=str(tmp_path)))
    initial = wired.goals.update_goal("project", SetGoalAction(text="Work"))
    if attribution == "model":
        # Current goal declarations deny a model pause before any state change.
        with pytest.raises(ValueError, match="cannot take goal action 'paused'"):
            wired.goals.update_goal(
                "project",
                PausedGoalAction(expect=GoalPrecondition(goal_id=initial.id)),
                actor=ModelInvocable,
            )
        assert wired.registry.require("project").goal == initial
    paused = wired.goals.update_goal(
        "project", PausedGoalAction(expect=GoalPrecondition(goal_id=initial.id))
    )
    blocked = wired.goals.block_goal_after_failed_turn(
        "project", started_goal=initial, expected_worktree=str(tmp_path), diagnostic="Failed"
    )
    assert blocked.state.declared_name == "blocked"
    assert blocked.revision == paused.revision + 1
    assert blocked.state.reason == "Failed"


def test_stale_owner_pause_does_not_bypass_failure_block(wired, tmp_path):
    wired.registry.declare(wired.registry.new_thread(name='project', tags=frozenset(), worktree=str(tmp_path)))
    initial = wired.goals.update_goal("project", SetGoalAction(text="Work"))
    wired.goals.update_goal(
        "project",
        PausedGoalAction(expect=GoalPrecondition(goal_id=initial.id)),
        actor=OwnerInvocable,
    )
    wired.goals.update_goal(
        "project",
        ActiveGoalAction(expect=GoalPrecondition(goal_id=initial.id)),
        actor=OwnerInvocable,
    )
    paused = wired.goals.update_goal(
        "project", PausedGoalAction(expect=GoalPrecondition(goal_id=initial.id))
    )
    blocked = wired.goals.block_goal_after_failed_turn(
        "project", started_goal=initial, expected_worktree=str(tmp_path), diagnostic="Failed"
    )
    assert blocked.state.declared_name == "blocked"
    assert blocked.revision == paused.revision + 1
    assert blocked.state.reason == "Failed"
