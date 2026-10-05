"""Current goal consumers over the original certified private-bus producer."""

import os

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    ActiveGoalAction,
    ClearGoalAction,
    CompletedGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.goal_states import ActiveGoal
from agent_comms.goals import Goal
from agent_comms.native_input_owner import RegistryOwner
from agent_comms.threads import Thread
from agent_comms.tools import CommsEditGoalTool, CommsResumeGoalTool
from goal_owner_fixture import canonical_goal_wire


@pytest.fixture
def selected_goal(tmp_path):
    comms = canonical_goal_wire(tmp_path / "wire")
    for name in ("worker", "peer", "other"):
        comms.registry.declare(Thread(
            name, frozenset(), str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        ))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Original objective"))
    return comms, goal


@pytest.mark.parametrize("replacement", [True, False])
def test_review_and_command_refuse_replaced_or_cleared_goal(selected_goal, replacement):
    comms, original = selected_goal
    current = comms.goals.update_goal(
        "worker", SetGoalAction(text="Different objective") if replacement else ClearGoalAction()
    )
    before = comms.registry.require("worker")
    with pytest.raises(ValueError, match="replaced or cleared"):
        comms.goals.goal_input_review("worker", original.id, ["peer"])
    with pytest.raises(ValueError, match="replaced or cleared"):
        comms.goals.update_goal("worker", CompletedGoalAction(
            expect=GoalPrecondition(goal_id=original.id),
        ))
    assert comms.registry.require("worker") == before
    assert before.goal == current
    assert before.goal_for(original.id) is None


def test_failed_turn_retains_later_progress_of_same_goal(selected_goal):
    comms, original = selected_goal
    later = comms.goals.update_goal("worker", ActiveGoalAction(
        expect=GoalPrecondition(goal_id=original.id), progress="Newer verified work",
    ))
    assert later.revision > original.revision
    assert comms.registry.require("worker").goal_for(original.id) == later
    result = comms.goals.block_goal_after_failed_turn(
        "worker", started_goal=original,
        expected_worktree=comms.registry.require("worker").worktree,
        diagnostic="Original turn failed after the newer report",
    )
    assert result.id == original.id
    assert result.progress.startswith(later.progress + "\n\n")
    assert result.state.toggle is not None
    assert result.state.toggle.declared_name == "retry"
    replacement = comms.goals.update_goal("worker", SetGoalAction(text="Replacement"))
    assert comms.goals.block_goal_after_failed_turn(
        "worker", started_goal=original,
        expected_worktree=comms.registry.require("worker").worktree,
        diagnostic="Must not apply to the replacement",
    ) == replacement


def test_exact_cas_and_owner_pause_remain_strict(selected_goal):
    comms, original = selected_goal
    later = comms.goals.update_goal("worker", ActiveGoalAction(progress="Later revision"))
    with pytest.raises(ValueError, match="changed during resume"):
        comms.goals.update_goal("worker", CompletedGoalAction(
            expect=GoalPrecondition(expected_goal=original, goal_id=original.id),
        ))
    assert comms.registry.require("worker").goal == later
    paused = comms.goals.update_goal(
        "worker", PausedGoalAction(expect=GoalPrecondition(goal_id=original.id)),
        actor=OwnerInvocable,
    )
    for operation in (
        lambda: comms.goals.goal_input_review("worker", original.id, ["peer"]),
        lambda: comms.goals.update_goal("worker", ActiveGoalAction(
            expect=GoalPrecondition(goal_id=original.id, expected_state=ActiveGoal()),
        )),
    ):
        with pytest.raises(ValueError, match="paused by the owner.*Do not resume"):
            operation()
        assert comms.registry.require("worker").goal == paused


def test_model_edit_and_resume_use_current_goal_selection(selected_goal, monkeypatch):
    comms, original = selected_goal
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    edited = CommsEditGoalTool(goal_id=original.id, text="Edited objective").apply(comms)
    current = FieldCodec.decode(Goal, edited["goal"])
    assert current.id == original.id and current.revision > original.revision
    comms.goals.update_goal("worker", PausedGoalAction())
    resumed = CommsResumeGoalTool(goal_id=original.id, progress="Explicit model resume").apply(comms)
    assert FieldCodec.decode(Goal, resumed["goal"]).id == original.id
    assert comms.registry.require("worker").goal.state.active
    replacement = comms.goals.update_goal("worker", SetGoalAction(text="Replacement"))
    with pytest.raises(ValueError, match="replaced or cleared"):
        CommsEditGoalTool(goal_id=original.id, text="Stale edit").apply(comms)
    with pytest.raises(ValueError, match="cannot be resumed"):
        CommsResumeGoalTool(goal_id=original.id, progress="Stale resume").apply(comms)
    assert comms.registry.require("worker").goal == replacement


def test_certified_reply_consumes_only_original_wait(selected_goal):
    comms, original = selected_goal
    peer = comms.agents.begin_turn("peer", "original-peer-work").require_turn_lease()
    owner = None
    try:
        comms.goals.update_goal("worker", StandbyGoalAction(
            expect=GoalPrecondition(goal_id=original.id), wait_for=("peer",),
        ))
        wait = comms.goals.goal_wait("worker")
        wrong = comms.messaging.send_message("peer", "other", "Different recipient")
        reply = comms.messaging.send_message("peer", "worker", "Original reply")
        owner = comms.agents.begin_turn("worker", "original-reply-consumer").require_turn_lease()
        captured = RegistryOwner.capture_local(comms.registry.snapshot(), "worker")
        assert not comms.goals.consume_reply_wait(captured, wrong.reference)
        assert comms.goals.goal_wait("worker") == wait
        assert comms.goals.consume_reply_wait(captured, reply.reference)
        assert comms.goals.goal_wait("worker") is None
        assert not comms.goals.consume_reply_wait(captured, reply.reference)
    finally:
        if owner is not None:
            comms.agents.finish_turn(owner)
        comms.agents.finish_turn(peer)
    assert comms.registry.require("worker").active_turn is None
    assert comms.registry.require("peer").active_turn is None

