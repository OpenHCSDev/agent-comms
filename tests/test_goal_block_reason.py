"""Blocking is explicit and inspectable; old rows are not assigned invented reasons."""

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    ActiveGoalAction,
    BlockedGoalAction,
    CompletedGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.goal_presentation import GoalExecution
from agent_comms.goal_states import UnrecordedBlockGoal
from agent_comms.goals import Goal
from agent_comms.threads import Thread
from agent_comms.tools import TOOLS


def _owner(tmp_path):
    comms = wire(tmp_path)
    comms.threads.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Finish selected work"))
    assert goal is not None
    return comms, goal


def test_missing_or_blank_block_reason_rejects_without_side_effects(tmp_path, monkeypatch):
    comms, original = _owner(tmp_path)
    progressed = comms.goals.update_goal(
        "worker", ActiveGoalAction(progress="Verified 2 of 3 items")
    )
    assert progressed is not None
    before_history = comms.goals.goal_history("worker", goal_id=original.id)
    before_registry = (tmp_path / "registry.json").read_bytes()
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    tool = next(tool for tool in TOOLS if tool.name == "comms_goal")

    for kwargs in (
        {},
        {"progress": " \n "},
        {"block_reason": "  ", "progress": "old progress must not become a reason"},
        {"block_reason": "x" * 1025},
    ):
        with pytest.raises(ValueError, match="reason"):
            comms.goals.update_goal(
                "worker", BlockedGoalAction(expect=GoalPrecondition(goal_id=original.id), **kwargs)
            )
        assert comms.registry.require("worker").goal == progressed
        assert (tmp_path / "registry.json").read_bytes() == before_registry
        assert comms.goals.goal_history("worker", goal_id=original.id) == before_history

    with pytest.raises(ValueError, match="nonempty reason"):
        tool.invoke(
            comms,
            {"goal_id": original.id, "status": "blocked", "progress": " \t "},
        )
    assert (tmp_path / "registry.json").read_bytes() == before_registry


def test_block_reason_round_trips_goal_execution_history_and_tool(tmp_path, monkeypatch):
    comms, original = _owner(tmp_path)
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    tool = next(tool for tool in TOOLS if tool.name == "comms_goal")
    reason = "Need the exact ACP error text from the user before diagnosing."
    result = tool.invoke(
        comms, {"goal_id": original.id, "status": "blocked", "progress": f"  {reason}  "}
    )
    blocked = wire(tmp_path).registry.require("worker").goal
    assert blocked is not None and blocked.state.declared_name == "blocked"
    assert blocked.state.reason == reason
    assert FieldCodec.decode(Goal, result["goal"]).state.reason == reason
    assert result["goal_execution"]["block_reason"] == reason
    assert comms.goals.goal_history("worker", goal_id=original.id)[-1].after == blocked

    execution = comms.goals.goal_execution("worker")
    assert execution is not None and execution.block_reason == reason
    assert "Blocked · Need the exact ACP error text" in execution.presentation("worker").summary
    assert GoalExecution.from_wire(result["goal_execution"]) == execution
    assert (
        FieldCodec.decode(Goal, comms.views.thread_detail("worker")["goal"]).state.reason == reason
    )


def test_automatic_blocks_record_diagnostic_not_prior_progress(tmp_path):
    comms, started = _owner(tmp_path)
    progressed = comms.goals.update_goal("worker", ActiveGoalAction(progress="Verified step"))
    assert progressed is not None
    blocked = comms.goals.block_goal_after_failed_turn(
        "worker",
        started_goal=started,
        expected_worktree=str(tmp_path),
        diagnostic="Attempt outcome uncertain; inspect before Retry.",
    )
    assert blocked is not None
    assert blocked.progress == "Verified step\n\nAttempt outcome uncertain; inspect before Retry."
    assert blocked.state.reason == "Attempt outcome uncertain; inspect before Retry."
    assert wire(tmp_path).goals.goal_execution("worker").block_reason == blocked.state.reason

    comms2, started2 = _owner(tmp_path / "another")
    completed = comms2.goals.update_goal(
        "worker", CompletedGoalAction(progress="Reported completion")
    )
    assert completed is not None
    revoked = comms2.goals.block_unverified_goal_completion(
        "worker",
        expected_goal=completed,
        expected_worktree=str(tmp_path / "another"),
        diagnostic="Provider turn failed after provisional completion.",
    )
    assert revoked is not None
    assert revoked.state.reason == "Provider turn failed after provisional completion."
    assert wire(tmp_path / "another").registry.require("worker").goal == revoked


def test_unrecorded_block_is_labeled_unavailable_not_inferred_from_progress(tmp_path):
    comms, goal = _owner(tmp_path)
    thread = comms.registry.require("worker")
    recorded = Goal(
        goal.text, goal.id, state=UnrecordedBlockGoal(), progress="Unrelated old progress"
    )
    comms.registry.register(replace(thread, goal=recorded), comms.registry.status("worker"))
    observed = wire(tmp_path).goals.goal_execution("worker")
    assert observed is not None
    assert observed.block_reason is None
    assert observed.presentation("worker").summary == "Blocked · reason unavailable"
    assert "Unrelated old progress" not in observed.presentation("worker").summary


def test_owner_resume_refusal_persists_bounded_reason_and_prior_progress(tmp_path):
    from agent_comms.goal_attempts import GoalAttemptStore

    comms, original = _owner(tmp_path)
    comms.threads.register(
        replace(
            comms.registry.require("worker"), process_identity=ProcessIdentity.capture(os.getpid())
        )
    )
    progressed = comms.goals.update_goal(
        "worker", PausedGoalAction(progress="Half verified by the owner")
    )
    assert progressed is not None
    private = tmp_path / "goal-private"
    private.mkdir(mode=0o700)
    store = GoalAttemptStore.initialize(private)
    store.create_goal(original.id)
    reservation = store.reserve(original.id, 1)
    store.record_failed(reservation, "Previous goal attempt failed")

    refusal = (
        "The interrupted goal attempt is unresolved. Inspect it, then use "
        "Retry to authorize a new attempt. Your messages can still be sent."
    )
    with pytest.raises(ValueError, match="interrupted goal attempt is unresolved"):
        comms.goals.update_goal(
            "worker",
            ActiveGoalAction(
                expect=GoalPrecondition(expected_owner_pid=os.getpid(), goal_id=original.id)
            ),
            actor=OwnerInvocable,
            owner_store=store,
        )
    reloaded = wire(tmp_path).registry.require("worker").goal
    assert reloaded is not None and reloaded.state.declared_name == "blocked"
    # Prior progress is retained; the refusal is stored separately as the reason.
    assert reloaded.progress == "Half verified by the owner"
    assert reloaded.state.reason == refusal
    execution = comms.goals.goal_execution("worker")
    assert execution is not None and execution.block_reason == refusal
    assert "Blocked · The interrupted goal attempt" in execution.presentation("worker").summary
    assert comms.goals.goal_history("worker", goal_id=original.id)[-1].after == reloaded
