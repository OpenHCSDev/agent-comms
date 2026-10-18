"""Owner intent survives failed execution; private attempt fences still fail closed."""

import json

import pytest

import agent_comms.agent_events as ae
from agent_comms.goal_actions import (
    ActiveGoalAction,
    GoalPrecondition,
    ModelInvocable,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.goal_attempts import UnresolvedAttemptError
from agent_comms.goal_generation import BlockedGeneration
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent
from test_acp import TestAgentTurn as GoalFixture
from stored_inputs import stored_inputs


@pytest.mark.parametrize("outcome", ["failed_done", "missing_done"])
@pytest.mark.parametrize("pause_timing", ["before_terminal", "during_block"])
async def test_failed_attempt_preserves_explicit_owner_pause(
    wired, tmp_path, monkeypatch, outcome, pause_timing
):
    agent = canonical_agent(wired, agent_bin="pi")
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _session: None)
    await agent.new_session(str(tmp_path / "project"))
    goal = wired.goals.update_goal("project", SetGoalAction(text="Continue independent work"))
    GoalFixture()._authorize_test_goal(agent, wired, goal)
    agent.inputs.dispositions.record(
        "acp:earlier-uncertain",
        seq=None,
        owner="project",
        claim_scope=wired.registry.snapshot().claimed_generations["project"],
        target="project",
        text="Do not replay this input",
    )
    ledger_before = stored_inputs(agent.inputs.dispositions.root)
    paused = None

    def owner_pause():
        nonlocal paused
        paused = wired.goals.update_goal(
            "project",
            PausedGoalAction(
                expect=GoalPrecondition(goal_id=goal.id),
                progress="Explicit owner pause during backend execution",
            ),
            actor=OwnerInvocable,
        )

    original_block = wired.goals.block_goal_after_failed_turn

    def pause_at_block(name, **kwargs):
        if paused is None:
            owner_pause()
        return original_block(name, **kwargs)

    if pause_timing == "during_block":
        # Race after ACP's active-goal precheck, before the wire-locked operation.
        monkeypatch.setattr(wired.goals, "block_goal_after_failed_turn", pause_at_block)

    async def failed_events(*args, **kwargs):
        if pause_timing == "before_terminal":
            owner_pause()
        yield ae.StreamSettled()
        if outcome == "failed_done":
            yield ae.Done(
                ok=False,
                text="Provider failed",
                reason_code="assistant_final_stop_missing",
                diagnostic={"exit_code": 0},
            )

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", failed_events)
    try:
        await agent.turns.run_agent_turn("project", "project", "Continue", autonomous_goal=True)
        assert paused is not None
        assert wired.registry.require("project").goal == paused
        assert wired.registry.require("project").goal.state.pause_source.instruction() is not None
        assert stored_inputs(agent.inputs.dispositions.root) == ledger_before
        assert (
            stored_inputs(agent.inputs.dispositions.root).rows["acp:earlier-uncertain"].declared_name
            == "unknown"
        )
        generation = wired.goals.open_goal_store().snapshot(goal.id)
        assert generation.lifecycle == BlockedGeneration() and generation.attempt_id is not None
        with pytest.raises(UnresolvedAttemptError):
            wired.goals.open_goal_store().resume(goal.id, generation.number)
        await agent.turns.goals.schedule_goal("project")
        assert not agent.inputs.state("project").pending_turns
        diagnostics = list((wired.root / "diagnostics").glob("*.json"))
        assert len(diagnostics) == 1
        diagnostic = json.loads(diagnostics[0].read_text())
        assert diagnostic["outcome"] == "failed; inputs must not be replayed automatically"
        if outcome == "failed_done":
            assert diagnostic["reason"] == "assistant_final_stop_missing"
            assert diagnostic["measurements"] == {"exit_code": 0}
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("attribution", ["model", "runtime"])
def test_nonowner_pause_does_not_bypass_failure_block(wired, tmp_path, attribution):
    wired.registry.declare(wired.registry.new_thread(name='project', tags=frozenset(), worktree=str(tmp_path)))
    initial = wired.goals.update_goal("project", SetGoalAction(text="Work"))
    if attribution == "model":
        # Current goal declarations deny a model pause before any state change.
        with pytest.raises(ValueError, match="cannot take goal action 'paused'"):
            wired.goals.update_goal(
                "project",
                PausedGoalAction(expect=GoalPrecondition(goal_id=initial.id)),
                actor=ModelInvocable,
            )
        assert wired.registry.require("project").goal == initial
    paused = wired.goals.update_goal(
        "project", PausedGoalAction(expect=GoalPrecondition(goal_id=initial.id))
    )
    blocked = wired.goals.block_goal_after_failed_turn(
        "project", started_goal=initial, expected_worktree=str(tmp_path), diagnostic="Failed"
    )
    assert blocked.state.declared_name == "blocked"
    assert blocked.revision == paused.revision + 1
    assert blocked.state.reason == "Failed"


def test_stale_owner_pause_does_not_bypass_failure_block(wired, tmp_path):
    wired.registry.declare(wired.registry.new_thread(name='project', tags=frozenset(), worktree=str(tmp_path)))
    initial = wired.goals.update_goal("project", SetGoalAction(text="Work"))
    wired.goals.update_goal(
        "project",
        PausedGoalAction(expect=GoalPrecondition(goal_id=initial.id)),
        actor=OwnerInvocable,
    )
    wired.goals.update_goal(
        "project",
        ActiveGoalAction(expect=GoalPrecondition(goal_id=initial.id)),
        actor=OwnerInvocable,
    )
    paused = wired.goals.update_goal(
        "project", PausedGoalAction(expect=GoalPrecondition(goal_id=initial.id))
    )
    blocked = wired.goals.block_goal_after_failed_turn(
        "project", started_goal=initial, expected_worktree=str(tmp_path), diagnostic="Failed"
    )
    assert blocked.state.declared_name == "blocked"
    assert blocked.revision == paused.revision + 1
    assert blocked.state.reason == "Failed"
