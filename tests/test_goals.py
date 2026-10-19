"""Persistent goal ownership, continuation, and stale-worker protection."""

import pytest

from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    ActiveGoalAction,
    BlockedGoalAction,
    ClearGoalAction,
    CompletedGoalAction,
    GoalAction,
    GoalPrecondition,
    PausedGoalAction,
    RetryGoalAction,
    SetGoalAction,
)
from agent_comms.goal_states import ActiveGoal, BlockedGoal, CompletedGoal, PausedGoal
from agent_comms.goals import (
    CountCheckResult, Goal, NoGoal, NoReportedTurn, ReportedTurn, ReportedTurnState, SilentCheckResult,
)
from agent_comms.task_sources import GoalTaskScope, TurnTaskScope
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import ToolRequest


def test_reported_turn_states_preserve_existing_wire_values():
    schema = {"anyOf": [{"type": "null"}, {"type": "string"}]}
    assert FieldCodec.value_schema(ReportedTurnState) == schema
    assert FieldCodec.record_schema(Goal)["properties"]["reported_turn"] == schema
    assert FieldCodec.value_schema(NoReportedTurn) == {"anyOf": [{"type": "null"}]}
    assert FieldCodec.value_schema(ReportedTurn) == {"anyOf": [{"type": "string"}]}
    absent = Goal('work', 'goal', published_seq=0)
    assert absent.reported_turn == NoReportedTurn()
    assert not absent.reports_turn('turn')
    assert FieldCodec.encode(absent)['reported_turn'] is None
    assert FieldCodec.decode(Goal, FieldCodec.encode(absent)) == absent
    reported = Goal('work', 'goal', published_seq=0, reported_turn=ReportedTurn('turn'))
    assert reported.reports_turn('turn') and not reported.reports_turn('other')
    assert FieldCodec.encode(reported)['reported_turn'] == 'turn'
    assert FieldCodec.decode(Goal, FieldCodec.encode(reported)) == reported


def test_check_require_uses_distance_and_outcome_count():
    from agent_comms.child_process import ChildResult, ExitedOutcome, SignaledOutcome

    silent = SilentCheckResult()
    silent.require(ChildResult(ExitedOutcome(0)))
    with pytest.raises(ValueError, match=r'1 away from SilentCheckResult\(\)'):
        silent.require(ChildResult(ExitedOutcome(0), b'noise'))
    count = CountCheckResult(2)
    count.require(ChildResult(ExitedOutcome(1), b'count 2\n'))
    with pytest.raises(ValueError, match=r'1 away from CountCheckResult\(expected=2\)'):
        count.require(ChildResult(ExitedOutcome(0), b'1\n'))
    with pytest.raises(ValueError, match='did not exit'):
        count.require(ChildResult(SignaledOutcome(15), b'2\n'))
    with pytest.raises(ValueError, match='printed no integer'):
        count.require(ChildResult(ExitedOutcome(0), b'no integer'))


def test_goal_survives_rename_and_reregistration(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Verify the release"))
    comms.threads.set_thread_configuration(comms.registry.require('worker'), model='test/model')
    comms.registry.declare(comms.registry.new_thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
    monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")
    comms.threads.rename_self("renamed", caller=ThreadCaller("worker"))
    restored = wire(tmp_path).registry.require("worker")
    assert restored.goal == goal
    assert restored.model == "test/model"
    assert restored.name == "renamed"
    comms.goals.update_goal("worker", PausedGoalAction())
    tool = next(
        tool for tool in ToolRequest.members_with(ToolRequest) if tool.declared_name == "comms_goal"
    )
    with pytest.raises(ValueError, match="no longer active"):
        tool.invoke(comms, {"goal_id": goal.id, "status": "active", "progress": "late update"}, caller=ThreadCaller("worker"))
    replacement = comms.goals.update_goal("worker", SetGoalAction(text="New objective"))
    with pytest.raises(ValueError, match="replaced"):
        comms.goals.update_goal(
            "worker", CompletedGoalAction(expect=GoalPrecondition(goal_id=goal.id))
        )
    assert comms.registry.require("worker").goal == replacement
    comms.goals.update_goal("worker", ClearGoalAction())
    assert wire(tmp_path).registry.require("worker").goal == NoGoal()
    with pytest.raises(ValueError, match="No goal"):
        comms.goals.update_goal("worker", ActiveGoalAction())
    with pytest.raises(ValueError, match="requires"):
        comms.goals.update_goal("worker", SetGoalAction(text=" "))
    with pytest.raises(ValueError, match="Unknown"):
        GoalAction.member_named("nonsense")


def test_no_goal_set_goal_and_cleared_goal_each_answer_from_the_member(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))

    def stored() -> Thread:
        return wire(tmp_path).registry.require("worker")

    def absent(thread: Thread) -> None:
        assert thread.goal == NoGoal()
        assert thread.wake_context(comms.goals)["current_goal"] is None
        assert thread.task_scope == TurnTaskScope(project=str(tmp_path))
        with pytest.raises(RelationViolationError, match="No goal is set"):
            thread.goal.require()

    absent(stored())
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Ship the release"))
    thread = stored()
    assert thread.goal.require() == thread.goal.require().require_id(goal.id) == goal
    assert thread.wake_context(comms.goals)["current_goal"] == {
        "text": "Ship the release", "status": "active", "progress": goal.progress,
    }
    scope = thread.task_scope
    assert scope == GoalTaskScope(project=str(tmp_path), goal=goal.revision_key)
    scope.require_context(thread)
    comms.goals.update_goal("worker", ClearGoalAction())
    absent(stored())
    with pytest.raises(RelationViolationError, match="No goal is set"):
        scope.require_context(stored())
    replacement = comms.goals.update_goal("worker", SetGoalAction(text="Ship the patch"))
    assert stored().task_scope == GoalTaskScope(project=str(tmp_path), goal=replacement.revision_key)
    with pytest.raises(RelationViolationError, match="replaced or cleared"):
        scope.require_context(stored())


def test_explicit_resume_tool_keeps_goal_id_and_rejects_stale_calls(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
    monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Finish the release"))
    comms.goals.update_goal("worker", PausedGoalAction(progress="Paused after an uncertain turn"))
    resume = next(
        tool
        for tool in ToolRequest.members_with(ToolRequest)
        if tool.declared_name == "comms_resume_goal"
    )

    result = resume.invoke(
        comms, {"goal_id": goal.id, "progress": "Explicitly resumed by the user"}, caller=ThreadCaller("worker")
    )
    assert result["goal"]["id"] == goal.id
    assert FieldCodec.decode(Goal, result["goal"]).state.declared_name == "active"
    assert comms.registry.require("worker").goal.progress == "Explicitly resumed by the user"

    with pytest.raises(ValueError, match="cannot be resumed"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale duplicate"}, caller=ThreadCaller("worker"))
    comms.goals.update_goal(
        "worker", BlockedGoalAction(block_reason="Need owner input before retry.")
    )
    with pytest.raises(ValueError, match="cannot be resumed"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale blocked update"}, caller=ThreadCaller("worker"))
    comms.goals.update_goal("worker", SetGoalAction(text="Replacement goal"))
    with pytest.raises(ValueError, match="goal was replaced or cleared"):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale replaced goal"}, caller=ThreadCaller("worker"))


@pytest.mark.parametrize(
    "state,action,label",
    [
        (ActiveGoal(), PausedGoalAction, "Pause"),
        (PausedGoal(), ActiveGoalAction, "Resume"),
        (BlockedGoal("Explicit fixture refusal"), RetryGoalAction, "Retry"),
        (CompletedGoal(), None, "Completed"),
    ],
)
def test_goal_control_distinguishes_resume_retry_and_completion(state, action, label):
    assert state.toggle is action
    assert state.toggle_label == label


def test_agent_can_set_its_own_persistent_goal(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
    monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")
    tool = next(
        tool
        for tool in ToolRequest.members_with(ToolRequest)
        if tool.declared_name == "comms_set_goal"
    )

    result = tool.invoke(comms, {"text": "Verify the autonomous workflow"}, caller=ThreadCaller("worker"))

    goal = comms.registry.require("worker").goal
    assert goal is not None
    assert goal.state.declared_name == "active"
    assert goal.text == "Verify the autonomous workflow"
    assert result["goal"]["id"] == goal.id
