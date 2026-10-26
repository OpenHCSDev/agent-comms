"""S8 extension experiments and canonical saved-data contracts."""

from agent_comms.repo import Checkout
import json
from dataclasses import replace

import pytest

from agent_comms.field_codec import GoalId
from agent_comms.agent_events import GoalUnchanged
from agent_comms.comms import wire
from agent_comms.goal_actions import (
    ActiveGoalAction,
    BlockedGoalAction,
    CompletedGoalAction,
    EditGoalAction,
    GoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.goal_states import (
    OwnerPause,
    PausedGoal,
)
from agent_comms.root_runtime import RootRuntime
from conftest import installed_comms
from agent_comms.threads import Thread


@pytest.fixture
def owner(tmp_path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(Thread(name='worker', tags=frozenset(), worktree=Checkout(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Keep the durable objective"))
    return comms, goal


@pytest.mark.parametrize(
    "action",
    [
        EditGoalAction(text="A revised objective"),
        PausedGoalAction(progress="Updated pause note"),
    ],
)
def test_experiment_c_every_pause_preserving_action_keeps_owner(owner, action):
    comms, goal = owner
    paused = comms.goals.update_goal("worker", PausedGoalAction(), actor=OwnerInvocable)
    changed = comms.goals.update_goal("worker", action, actor=OwnerInvocable)
    assert changed.id == paused.id and changed.revision == paused.revision + 1
    assert isinstance(changed.state, PausedGoal) and isinstance(changed.state.source, OwnerPause)
    # Immutable rewrites and cold reopen retain the original source in Goal.
    rewritten = replace(changed, progress="new report", revision=changed.revision + 1)
    comms.registry.register(replace(comms.registry.require("worker"), goal=rewritten))
    assert wire(comms.root).registry.require("worker").goal.state == paused.state
    assert comms.goals.goal_history("worker")[-1].after.state == paused.state


@pytest.mark.parametrize(
    "action",
    [ActiveGoalAction, StandbyGoalAction, CompletedGoalAction, BlockedGoalAction, PausedGoalAction],
)
def test_automated_transition_cannot_remove_owner_pause(owner, action):
    comms, _ = owner
    paused = comms.goals.update_goal("worker", PausedGoalAction(), actor=OwnerInvocable)
    with pytest.raises(ValueError):
        comms.goals.update_goal("worker", action(progress="reason"))
    assert comms.registry.require("worker").goal == paused


def test_cas_rejects_aba_and_unknown_payload_without_effects(owner):
    comms, goal = owner
    previous = comms.goals.goal_snapshot("worker")
    current = comms.goals.update_goal("worker", ActiveGoalAction(progress="same"))
    current = comms.goals.update_goal("worker", ActiveGoalAction(progress=""))
    assert current != goal
    action = GoalAction.from_payload({"kind": "completed", "progress": "done"})
    with pytest.raises(ValueError):
        comms.goals.update_goal(
            "worker", replace(action, expect=GoalPrecondition(expected_goal=goal))
        )
    with pytest.raises(ValueError):
        GoalAction.from_payload({"kind": "active", "text": "wrong action fields"})
    assert comms.registry.require("worker").goal == current
    assert comms.goals.goal_changed("worker", previous).snapshot.goal == current


def test_goal_changed_compares_goal_snapshots(owner):
    comms, _ = owner
    before = comms.goals.goal_snapshot("worker")
    assert isinstance(comms.goals.goal_changed("worker", before), GoalUnchanged)
    changed = comms.goals.update_goal("worker", ActiveGoalAction(progress="moved"))
    event = comms.goals.goal_changed("worker", before)
    assert event.snapshot == comms.goals.goal_snapshot("worker")
    assert event.snapshot.goal == changed
    assert isinstance(comms.goals.goal_changed("worker", event.snapshot), GoalUnchanged)


def test_original_proxy_decodes_typed_goal_control_at_json_boundary():
    original_goal_id = GoalId.mint()
    from agent_comms.runtime_requests import RuntimeRequest, UpdateGoalRuntimeRequest

    payload = UpdateGoalRuntimeRequest.proxy_payload("worker", None, {
        "status": PausedGoalAction, "goal_id": original_goal_id, "expected_revision": 7,
    })
    assert payload == {"action": "update_goal", "thread": "worker", "status": "paused",
                       "goal_id": original_goal_id, "expected_revision": 7}
    request = RuntimeRequest.from_wire(json.loads(json.dumps(payload)))
    assert request == UpdateGoalRuntimeRequest(thread="worker", status=PausedGoalAction,
        goal_id=original_goal_id, expected_revision=7)
    with pytest.raises(ValueError):
        RuntimeRequest.from_wire({**payload, "status": "unrecorded"})


def test_fresh_cli_tool_process_preserves_owner_pause(owner, source_programs):
    import subprocess

    from agent_comms.cli import main

    comms, goal = owner
    paused = comms.goals.update_goal("worker", PausedGoalAction(), actor=OwnerInvocable)
    RootRuntime.publish(comms.root, source_programs.runtime)
    result = subprocess.run(
        [
            source_programs.script(main),
            "--root",
            str(comms.root),
            "tool",
            "--from",
            "worker",
            "--tool",
            "comms_goal",
            "--arguments",
            json.dumps(
                {
                    "goal_id": goal.id,
                    "status": "active",
                    "progress": "stale worker report",
                }
            ),
        ],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1, result.stderr
    assert "paused by the owner" in json.loads(result.stdout)["error"]
    assert comms.registry.require("worker").goal == paused
