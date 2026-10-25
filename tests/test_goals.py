"""Persistent goal ownership, continuation, and stale-worker protection."""

import pytest

from agent_comms.comms import wire
from conftest import installed_comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, GoalId, TurnId
from agent_comms.goal_actions import (
    ActiveGoalAction,
    BlockedGoalAction,
    ClearGoalAction,
    CompletedGoalAction,
    GoalAction,
    GoalPrecondition,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.goals import Goal, NoGoal
from agent_comms.task_sources import GoalTaskScope, TurnTaskScope
from agent_comms.threads import ChosenModel, NextTurnConfiguration, Thread
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tool_request import ToolRequest
from agent_comms.thread_model import DeclaredModel
from agent_comms.reported_turn import NoReportedTurn, ReportedTurn, ReportedTurnState
from agent_comms.goal_scope import OneGoal


def test_reported_turn_states_preserve_existing_wire_values():
    goal_id = GoalId.mint()
    turn_id = TurnId.mint()
    other_turn = TurnId.mint()
    turn_schema = FieldCodec.value_schema(TurnId)
    schema = {"anyOf": [{"type": "null"}, turn_schema]}
    assert FieldCodec.value_schema(ReportedTurnState) == schema
    assert FieldCodec.record_schema(Goal)["properties"]["reported_turn"] == schema
    assert FieldCodec.value_schema(NoReportedTurn) == {"anyOf": [{"type": "null"}]}
    assert FieldCodec.value_schema(ReportedTurn) == {"anyOf": [turn_schema]}
    absent = Goal('work', goal_id, published_seq=0)
    assert absent.reported_turn == NoReportedTurn()
    assert not absent.reports_turn(ReportedTurn(turn_id))
    assert not absent.reports_turn(NoReportedTurn())
    assert FieldCodec.encode(absent)['reported_turn'] is None
    assert FieldCodec.decode(Goal, FieldCodec.encode(absent)) == absent
    reported = Goal('work', goal_id, published_seq=0, reported_turn=ReportedTurn(turn_id))
    assert reported.reports_turn(ReportedTurn(turn_id))
    assert not reported.reports_turn(ReportedTurn(other_turn))
    assert FieldCodec.encode(reported)['reported_turn'] == turn_id
    assert FieldCodec.decode(Goal, FieldCodec.encode(reported)) == reported




def test_goal_survives_rename_and_reregistration(tmp_path, monkeypatch):
    comms = installed_comms(tmp_path)
    comms.registry.declare(Thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Verify the release"))
    comms.threads.set_thread_configuration(
            comms.registry.require('worker'), NextTurnConfiguration(model=ChosenModel(DeclaredModel('test/model'))))
    comms.registry.declare(Thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
    monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")
    comms.threads.rename_self("renamed", caller=ThreadCaller("worker"))
    restored = wire(tmp_path).registry.require("worker")
    assert restored.goal == goal
    assert restored.model == DeclaredModel("test/model")
    assert restored.name == "renamed"
    comms.goals.update_goal("worker", PausedGoalAction())
    tool = next(
        tool for tool in ToolRequest.members_with(ToolRequest) if tool.declared_name == "comms_goal"
    )
    with pytest.raises(ValueError):
        tool.invoke(comms, {"goal_id": goal.id, "status": "active", "progress": "late update"}, caller=ThreadCaller("worker"))
    replacement = comms.goals.update_goal("worker", SetGoalAction(text="New objective"))
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "worker", CompletedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(goal.id)))
        )
    assert comms.registry.require("worker").goal == replacement
    comms.goals.update_goal("worker", ClearGoalAction())
    assert wire(tmp_path).registry.require("worker").goal == NoGoal()
    with pytest.raises(ValueError):
        comms.goals.update_goal("worker", ActiveGoalAction())
    with pytest.raises(ValueError):
        comms.goals.update_goal("worker", SetGoalAction(text=" "))
    with pytest.raises(ValueError):
        GoalAction.member_named("nonsense")


def test_no_goal_set_goal_and_cleared_goal_each_answer_from_the_member(tmp_path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(Thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))

    def stored() -> Thread:
        return wire(tmp_path).registry.require("worker")

    def absent(thread: Thread) -> None:
        assert thread.goal == NoGoal()
        from agent_comms.context_segments.wake_values import NoWakeGoalContext

        context = thread.wake_context(comms.goals)
        assert isinstance(context.current_goal, NoWakeGoalContext)
        assert FieldCodec.encode(context)["current_goal"] is None
        assert thread.task_scope == TurnTaskScope(project=str(tmp_path))
        with pytest.raises(RelationViolationError):
            thread.goal.require()

    absent(stored())
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Ship the release"))
    thread = stored()
    assert thread.goal.require() == thread.goal.require().require_id(goal.id) == goal
    from agent_comms.context_segments.wake_values import WakeGoalContext

    context = thread.wake_context(comms.goals)
    assert context.current_goal == WakeGoalContext(
        text="Ship the release", state=goal.state, progress=goal.progress,
    )
    assert FieldCodec.encode(context)["current_goal"] == {
        "text": "Ship the release", "status": {"kind": "active"}, "progress": goal.progress,
    }
    scope = thread.task_scope
    assert scope == GoalTaskScope(project=str(tmp_path), goal=goal.revision_key)
    scope.require_context(thread)
    comms.goals.update_goal("worker", ClearGoalAction())
    absent(stored())
    with pytest.raises(RelationViolationError):
        scope.require_context(stored())
    replacement = comms.goals.update_goal("worker", SetGoalAction(text="Ship the patch"))
    assert stored().task_scope == GoalTaskScope(project=str(tmp_path), goal=replacement.revision_key)
    with pytest.raises(RelationViolationError):
        scope.require_context(stored())


def test_explicit_resume_tool_keeps_goal_id_and_rejects_stale_calls(tmp_path, monkeypatch):
    comms = installed_comms(tmp_path)
    comms.registry.declare(Thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
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

    with pytest.raises(ValueError):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale duplicate"}, caller=ThreadCaller("worker"))
    comms.goals.update_goal(
        "worker", BlockedGoalAction(block_reason="Need owner input before retry.")
    )
    with pytest.raises(ValueError):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale blocked update"}, caller=ThreadCaller("worker"))
    comms.goals.update_goal("worker", SetGoalAction(text="Replacement goal"))
    with pytest.raises(ValueError):
        resume.invoke(comms, {"goal_id": goal.id, "progress": "stale replaced goal"}, caller=ThreadCaller("worker"))


def test_agent_can_set_its_own_persistent_goal(tmp_path, monkeypatch):
    comms = installed_comms(tmp_path)
    comms.registry.declare(Thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
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
