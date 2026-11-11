"""Blocking is explicit and inspectable; old rows are not assigned invented reasons."""

import os
from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.turn_failure import BackendFailed, FailedTurnResolution
from agent_comms.planning.goal_actions import (
    ActiveGoalAction,
    BlockedGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.planning.goal_scope import OneGoal
from agent_comms.planning.goal_states import UnrecordedBlockGoal
from agent_comms.planning.goals import Goal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import RegistryFile
from agent_comms.transport.request import ToolRequest
from agent_comms.transport.tool_caller import ThreadCaller
from conftest import installed_comms


def _owner(tmp_path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(NativeThread(name='worker', tags=frozenset(), worktree=Checkout(tmp_path)))
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
    before_registry = (RegistryFile.path_in(tmp_path)).read_bytes()
    monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")

    for kwargs in (
        {},
        {"progress": " \n "},
        {"block_reason": "  ", "progress": "old progress must not become a reason"},
        {"block_reason": "x" * 1025},
    ):
        with pytest.raises(ValueError):
            comms.goals.update_goal(
                "worker", BlockedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(original.id)), **kwargs)
            )
        assert comms.registry.require("worker").goal == progressed
        assert (RegistryFile.path_in(tmp_path)).read_bytes() == before_registry
        assert comms.goals.goal_history("worker", OneGoal(original.id)) == before_history

    with pytest.raises(ValueError):
        ToolRequest.member_named("comms_goal").invoke(
            comms,
            {"goal_id": original.id, "status": "blocked", "progress": " \t "}, caller=ThreadCaller("worker"),
        )
    assert (RegistryFile.path_in(tmp_path)).read_bytes() == before_registry


def test_unrecorded_block_is_labeled_unavailable_not_inferred_from_progress(tmp_path):
    comms, goal = _owner(tmp_path)
    thread = comms.registry.require("worker")
    recorded = Goal(
        goal.text, goal.id, published_seq=goal.published_seq, state=UnrecordedBlockGoal(), progress="Unrelated old progress"
    )
    comms.registry.register(replace(thread, goal=recorded), comms.registry.status("worker"))
    observed = wire(tmp_path).goals.goal_execution("worker")
    assert "reason unavailable" in observed.presentation("worker").summary
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
    reservation.fail(store, FailedTurnResolution(BackendFailed))

    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "worker",
            ActiveGoalAction(
                expect=GoalPrecondition(expected_owner=ProcessIdentity.capture(os.getpid()), goal_id=OneGoal(original.id))
            ),
            actor=OwnerInvocable,
        )
    reloaded = wire(tmp_path).registry.require("worker").goal
    assert reloaded == progressed
    assert reloaded.progress == "Half verified by the owner"
    execution = comms.goals.goal_execution("worker")
    assert execution.source == progressed.state.source
    assert comms.goals.goal_history("worker", OneGoal(original.id))[-1].after == reloaded
