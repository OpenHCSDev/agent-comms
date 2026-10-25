"""Blocking is explicit and inspectable; old rows are not assigned invented reasons."""

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from conftest import installed_comms
from agent_comms.goal_actions import (
    ActiveGoalAction,
    BlockedGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.goal_history import OneGoal
from agent_comms.goal_states import UnrecordedBlockGoal
from agent_comms.goal_presentation import PausedExecution, UnrecordedBlockExecution
from agent_comms.goals import Goal
from agent_comms.tool_request import ToolRequest
from agent_comms.tool_caller import ThreadCaller
from agent_comms.threads import Thread


def _owner(tmp_path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(Thread(name='worker', tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Finish selected work"))
    assert goal is not None
    return comms, goal


def test_missing_or_blank_block_reason_rejects_without_side_effects(tmp_path, monkeypatch):
    comms, original = _owner(tmp_path)
    progressed = comms.goals.update_goal(
        "worker", ActiveGoalAction(progress="Verified 2 of 3 items")
    )
    assert progressed is not None
    before_history = comms.goals.goal_history("worker", OneGoal(original.id))
    before_registry = (tmp_path / "registry.json").read_bytes()
    monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")
    tool = next(
        tool for tool in ToolRequest.members_with(ToolRequest) if tool.declared_name == "comms_goal"
    )

    for kwargs in (
        {},
        {"progress": " \n "},
        {"block_reason": "  ", "progress": "old progress must not become a reason"},
        {"block_reason": "x" * 1025},
    ):
        with pytest.raises(ValueError):
            comms.goals.update_goal(
                "worker", BlockedGoalAction(expect=GoalPrecondition(goal_id=original.id), **kwargs)
            )
        assert comms.registry.require("worker").goal == progressed
        assert (tmp_path / "registry.json").read_bytes() == before_registry
        assert comms.goals.goal_history("worker", OneGoal(original.id)) == before_history

    with pytest.raises(ValueError):
        tool.invoke(
            comms,
            {"goal_id": original.id, "status": "blocked", "progress": " \t "}, caller=ThreadCaller("worker"),
        )
    assert (tmp_path / "registry.json").read_bytes() == before_registry


def test_unrecorded_block_is_labeled_unavailable_not_inferred_from_progress(tmp_path):
    comms, goal = _owner(tmp_path)
    thread = comms.registry.require("worker")
    recorded = Goal(
        goal.text, goal.id, published_seq=goal.published_seq, state=UnrecordedBlockGoal(), progress="Unrelated old progress"
    )
    comms.registry.register(replace(thread, goal=recorded), comms.registry.status("worker"))
    observed = wire(tmp_path).goals.goal_execution("worker")
    assert isinstance(observed, UnrecordedBlockExecution)
    assert observed.presentation("worker").summary == "Blocked · reason unavailable"
    assert "Unrelated old progress" not in observed.presentation("worker").summary


def test_owner_resume_refusal_preserves_pause_and_prior_progress(tmp_path):

    comms, original = _owner(tmp_path)
    comms.registry.declare(
        replace(
            comms.registry.require("worker"), process_identity=ProcessIdentity.capture(os.getpid())
        )
    )
    progressed = comms.goals.update_goal(
        "worker", PausedGoalAction(progress="Half verified by the owner")
    )
    assert progressed is not None
    store = comms.goals.open_goal_store()
    reservation = store.reserve(original.id, 1)
    reservation.fail(store, "Previous goal attempt failed")

    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "worker",
            ActiveGoalAction(
                expect=GoalPrecondition(expected_owner=ProcessIdentity.capture(os.getpid()), goal_id=original.id)
            ),
            actor=OwnerInvocable,
        )
    reloaded = wire(tmp_path).registry.require("worker").goal
    assert reloaded == progressed
    assert reloaded.progress == "Half verified by the owner"
    execution = comms.goals.goal_execution("worker")
    assert isinstance(execution, PausedExecution)
    assert execution.source == progressed.state.source
    assert comms.goals.goal_history("worker", OneGoal(original.id))[-1].after == reloaded
