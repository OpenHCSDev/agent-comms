"""Owner pause, goal CAS and goal-control wire contracts."""

import json
import subprocess
from dataclasses import replace

import pytest

from agent_comms.cli import main
from agent_comms.comms import wire
from agent_comms.execution.root_runtime import RootRuntime
from agent_comms.nominal.field_codec import FieldCodec, GoalId
from agent_comms.planning.goal_actions import (
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
from agent_comms.planning.goal_events import GoalChanged, GoalUnchanged
from agent_comms.planning.goal_presentation import UnpublishedGoalSnapshot
from agent_comms.planning.goal_states import OwnerPause, PausedGoal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.owner_connection import CapturedOwner
from agent_comms.transport.runtime_request import RuntimeRequest
from agent_comms.transport.runtime_requests import UpdateGoalRuntimeRequest
from conftest import installed_comms


@pytest.fixture
def owner(tmp_path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(NativeThread(name='worker', tags=frozenset(), worktree=Checkout(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Keep the durable objective"))
    return comms, goal


@pytest.mark.parametrize(
    "action",
    [
        EditGoalAction(text="A revised objective"),
        PausedGoalAction(progress="Updated pause note"),
    ],
)
def test_every_pause_preserving_action_keeps_owner(owner, action):
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
            "worker", replace(action, expect=GoalPrecondition(expected_checkpoint=goal.captured))
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


def test_owner_declaration_routes_without_claiming_complete_projection_equality(owner):
    comms, goal = owner
    assert UnpublishedGoalSnapshot().goal_changed(goal)
    published = comms.goals.goal_snapshot("worker")
    assert not published.goal_changed(goal)
    changed = wire(comms.root).goals.update_goal("worker", ActiveGoalAction(progress="external"))
    assert published.goal_changed(changed)
    assert comms.goals.goal_changed("worker", published) == GoalChanged(
        comms.goals.goal_snapshot("worker"))


def test_unrelated_attempt_commit_requests_a_real_comparison_not_a_second_revision(owner):
    comms, _ = owner
    published = comms.goals.goal_snapshot("worker")
    before = comms.registry.store.path.read_bytes()
    comms.goals.open_goal_store().create_goal(GoalId.mint())
    assert comms.registry.store.path.read_bytes() == before
    assert isinstance(comms.goals.goal_changed("worker", published), GoalUnchanged)


def test_original_proxy_decodes_typed_goal_control_at_json_boundary():
    original_goal_id = GoalId.mint()
    worker = RecordedThreadIncarnation("worker", 12.5)
    payload = UpdateGoalRuntimeRequest.proxy_payload(worker, None, {
        "status": PausedGoalAction, "goal_id": original_goal_id, "expected_revision": 7,
    })
    assert payload == {"action": "update_goal", "thread": "worker",
                       "captured": {"name": "worker", "created_at": 12.5}, "status": "paused",
                       "goal_id": original_goal_id, "expected_revision": 7}
    request = FieldCodec.decode(RuntimeRequest, json.loads(json.dumps(payload)))
    assert request == UpdateGoalRuntimeRequest(thread="worker", captured=CapturedOwner(worker),
        status=PausedGoalAction, goal_id=original_goal_id, expected_revision=7)
    with pytest.raises(ValueError):
        FieldCodec.decode(RuntimeRequest, {**payload, "status": "unrecorded"})


def test_fresh_cli_tool_process_preserves_owner_pause(owner, source_programs):
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
    assert comms.registry.require("worker").goal == paused
