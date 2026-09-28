"""S8 extension experiments and canonical saved-data contracts."""

import importlib
import json
from dataclasses import dataclass, replace

import pytest

from agent_comms import tools
from agent_comms.agent_event_updates import AcpEventConsumer
from agent_comms.agent_events import GoalChanged
from agent_comms.comms import wire
from agent_comms.goal_actions import (
    ActiveGoalAction,
    BlockedGoalAction,
    CompletedGoalAction,
    EditGoalAction,
    GoalAction,
    GoalActionContext,
    GoalPrecondition,
    ModelInvocable,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
    TransitionGoalAction,
)
from agent_comms.goal_states import (
    OwnerPause,
    PausedGoal,
    PauseSource,
)
from agent_comms.threads import Thread


@pytest.fixture
def owner(tmp_path):
    comms = wire(tmp_path)
    comms.threads.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Keep the durable objective"))
    return comms, goal


def test_experiment_a_one_new_pause_source_carries_all_behavior(owner, monkeypatch):
    comms, goal = owner
    monkeypatch.setattr(PauseSource, "__registry__", dict(PauseSource.__registry__))

    class SpendCapPause(PauseSource):
        def instruction(self):
            return "Spend cap reached; wait for the owner to authorize more work."

    paused = replace(goal, state=PausedGoal(SpendCapPause()), revision=2)
    comms.registry.register(replace(comms.registry.require("worker"), goal=paused))
    edited = comms.goals.update_goal("worker", EditGoalAction(text="Revised objective"))
    assert edited.state.source.declared_name == "spend_cap"
    assert comms.goals.goal_pause("worker").owner_instruction == SpendCapPause().instruction()
    assert wire(comms.root).registry.require("worker").goal.state.source == SpendCapPause()
    failed = comms.goals.block_goal_after_failed_turn(
        "worker", started_goal=goal, expected_worktree=str(comms.root), diagnostic="Backend failed"
    )
    assert failed == edited
    with pytest.raises(ValueError, match="Spend cap reached"):
        comms.goals.update_goal("worker", ActiveGoalAction())


async def test_experiment_b_new_model_action_uses_real_tool_and_event(owner, monkeypatch):
    comms, goal = owner
    monkeypatch.setattr(GoalAction, "__registry__", dict(GoalAction.__registry__))

    @dataclass(frozen=True, kw_only=True)
    class CheckpointGoalAction(TransitionGoalAction, ModelInvocable):
        def next_state(self, ctx: GoalActionContext):
            return ctx.require_goal().state

    # Production declarations load before the tool catalog. Rebuild that catalog
    # in this test to simulate adding just the new declaration at startup.
    importlib.reload(tools)
    try:
        tool = next(t for t in tools.TOOLS if t.name == "comms_goal")
        assert "checkpoint" in tool.schema()["parameters"]["properties"]["status"]["enum"]
        monkeypatch.setenv("PI_AGENT_ID", "worker")
        previous = comms.goals.goal_snapshot("worker")
        tool.invoke(
            comms, {"status": "checkpoint", "goal_id": goal.id, "progress": "Evidence recorded"}
        )
        event = wire(comms.root).goals.goal_changed("worker", previous)
        assert isinstance(event, GoalChanged)
        assert event.goal.progress == "Evidence recorded"
        updates = []

        class Client:
            async def session_update(self, **kwargs):
                updates.append(kwargs)

        await AcpEventConsumer(None, "worker", Client()).dispatch(event)
        assert updates[0]["update"].field_meta["agentComms"]["goal"]["revision"] == 2
        assert comms.goals.goal_changed("worker", event.signature) is None
    finally:
        GoalAction.__registry__.pop("checkpoint")
        importlib.reload(tools)


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
    # Prove the current state survives without its audit document and through
    # both ordinary immutable rewrites and a new process-style registry load.
    (comms.root / "goal_pause_events.json").unlink()
    rewritten = replace(changed, progress="new report", revision=changed.revision + 1)
    comms.registry.register(replace(comms.registry.require("worker"), goal=rewritten))
    assert wire(comms.root).registry.require("worker").goal.state == paused.state
    assert comms.goals.goal_history("worker")[-1].after.state == paused.state
    assert (
        comms.goals.block_goal_after_failed_turn(
            "worker",
            started_goal=goal,
            expected_worktree=str(comms.root),
            diagnostic="Backend failed",
        )
        == rewritten
    )


@pytest.mark.parametrize(
    "action",
    [ActiveGoalAction, StandbyGoalAction, CompletedGoalAction, BlockedGoalAction, PausedGoalAction],
)
def test_automated_transition_cannot_remove_owner_pause(owner, action):
    comms, _ = owner
    paused = comms.goals.update_goal("worker", PausedGoalAction(), actor=OwnerInvocable)
    with pytest.raises(ValueError, match="paused by the owner"):
        comms.goals.update_goal("worker", action(progress="reason"))
    assert comms.registry.require("worker").goal == paused


def test_cas_rejects_aba_and_unknown_payload_without_effects(owner):
    comms, goal = owner
    previous = comms.goals.goal_snapshot("worker")
    current = comms.goals.update_goal("worker", ActiveGoalAction(progress="same"))
    current = comms.goals.update_goal("worker", ActiveGoalAction(progress=""))
    assert current != goal
    action = GoalAction.from_payload({"kind": "completed", "progress": "done"})
    with pytest.raises(ValueError, match="changed during resume"):
        comms.goals.update_goal(
            "worker", replace(action, expect=GoalPrecondition(expected_goal=goal))
        )
    with pytest.raises(ValueError, match="Unknown fields"):
        GoalAction.from_payload({"kind": "active", "text": "wrong action fields"})
    assert comms.registry.require("worker").goal == current
    assert comms.goals.goal_changed("worker", previous).goal == current


def test_fresh_cli_tool_process_preserves_owner_pause(owner):
    import os
    import subprocess
    import sys

    comms, goal = owner
    paused = comms.goals.update_goal("worker", PausedGoalAction(), actor=OwnerInvocable)
    environment = {**os.environ, "PI_AGENT_ID": "worker"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_comms.cli",
            "--root",
            str(comms.root),
            "invoke",
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
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1, result.stderr
    assert "paused by the owner" in json.loads(result.stdout)["error"]
    assert wire(comms.root).registry.require("worker").goal == paused
