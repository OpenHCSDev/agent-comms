"""Goals are shared responsibility: a thread clears its own goal, and a teammate reads and
changes another thread's goal, except under a protected pause."""
from pathlib import Path

import pytest

from agent_comms.nominal.field_codec import FieldCodec, GoalId
from agent_comms.planning.goal_actions import (
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
)
from agent_comms.planning.goal_scope import OneGoal
from agent_comms.planning.goal_states import (
    ModelPause,
    PausedGoal,
)
from agent_comms.planning.goals import NoGoal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.served import invoke_tool

OWNER, PEER = ThreadCaller("owner"), ThreadCaller("peer")


def _owner_goal(comms) -> dict:
    for name in ("owner", "peer"):
        comms.registry.declare(NativeThread(name=name, tags=frozenset(), worktree=Checkout(Path("/wt"))))
    return invoke_tool(comms, "comms_set_goal", {"text": "keep working"}, caller=OWNER)["goal"]


def test_a_thread_clears_its_own_goal(comms):
    started = _owner_goal(comms)
    invoke_tool(comms, "comms_clear_goal", {"goal_id": started["id"]}, caller=OWNER)
    assert comms.registry.require("owner").goal == NoGoal()


def test_a_teammate_reads_and_changes_another_threads_goal(comms):
    started = _owner_goal(comms)
    history = invoke_tool(comms, "comms_goal_history", {"thread": "owner"}, caller=PEER)
    assert started["id"] in str(history)

    edited = invoke_tool(comms, "comms_edit_goal", {
        "thread": "owner", "goal_id": started["id"], "text": "keep working, with tests"},
        caller=PEER)
    assert edited["thread"] == "owner"
    assert comms.registry.require("owner").goal.text == "keep working, with tests"

    invoke_tool(comms, "comms_pause_goal", {
        "thread": "owner", "goal_id": started["id"], "progress": "waiting on the install"},
        caller=PEER)
    assert comms.registry.require("owner").goal.state == PausedGoal(ModelPause())

    invoke_tool(comms, "comms_resume_goal", {
        "thread": "owner", "goal_id": started["id"], "progress": "install landed"}, caller=PEER)
    comms.registry.require("owner").goal.require_active()

    invoke_tool(comms, "comms_clear_goal", {"thread": "owner", "goal_id": started["id"]},
                caller=PEER)
    assert comms.registry.require("owner").goal == NoGoal()
    assert comms.registry.require("peer").goal == NoGoal()


def test_a_teammate_cannot_clear_a_goal_tristan_paused(comms):
    started = _owner_goal(comms)
    goal_id = FieldCodec.decode(GoalId, started["id"])
    paused = comms.goals.update_goal(
        "owner", PausedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(goal_id))),
        actor=OwnerInvocable)
    with pytest.raises(ValueError, match="cannot change this goal while it is paused"):
        invoke_tool(comms, "comms_clear_goal", {"thread": "owner", "goal_id": started["id"]},
                    caller=PEER)
    assert comms.registry.require("owner").goal == paused
    invoke_tool(comms, "comms_clear_goal", {"goal_id": started["id"]}, caller=OWNER)
    assert comms.registry.require("owner").goal == NoGoal()
