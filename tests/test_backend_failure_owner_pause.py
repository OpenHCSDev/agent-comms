"""Owner intent survives failed execution; private attempt fences still fail closed."""

import json

import pytest

from agent_comms import Thread
from agent_comms.acp import CommsAgent
from agent_comms.goal_attempts import UnresolvedAttempt
from test_acp import TestAgentTurn as GoalFixture


@pytest.mark.parametrize("outcome", ["failed_done", "missing_done"])
@pytest.mark.parametrize("pause_timing", ["before_terminal", "during_block"])
async def test_failed_attempt_preserves_explicit_owner_pause(
    wired, tmp_path, monkeypatch, outcome, pause_timing
):
    agent = CommsAgent(wired, agent_bin="pi")
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    await agent.new_session(str(tmp_path / "project"))
    goal = wired.update_goal("project", "set", text="Continue independent work")
    GoalFixture()._authorize_test_goal(agent, wired, goal)
    agent._dispositions.record(
        "acp:earlier-uncertain",
        seq=None,
        owner="project",
        admission=wired.registry.snapshot().admission_generations["project"],
        target="project",
        text="Do not replay this input",
    )
    ledger_before = agent._dispositions.path.read_bytes()
    paused = None
    pause_bytes = None

    def owner_pause():
        nonlocal paused, pause_bytes
        paused = wired.update_goal(
            "project",
            "paused",
            goal_id=goal.id,
            owner_action=True,
            progress="Explicit owner pause during backend execution",
        )
        pause_bytes = (wired.root / "goal_pause_events.json").read_bytes()

    original_block = wired.block_goal_after_failed_turn

    def pause_at_block(name, **kwargs):
        if paused is None:
            owner_pause()
        return original_block(name, **kwargs)

    if pause_timing == "during_block":
        # Race after ACP's active-goal precheck, before the wire-locked operation.
        monkeypatch.setattr(wired, "block_goal_after_failed_turn", pause_at_block)

    async def failed_events(*args, **kwargs):
        if pause_timing == "before_terminal":
            owner_pause()
        yield {"type": "settled"}
        if outcome == "failed_done":
            yield {
                "type": "done",
                "ok": False,
                "text": "Provider failed",
                "reason_code": "assistant_final_stop_missing",
                "diagnostic": {"exit_code": 0},
            }

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", failed_events)
    try:
        await agent._run_agent_turn("project", "project", "Continue", autonomous_goal=True)
        assert paused is not None
        assert wired.registry.require("project").goal == paused
        assert (wired.root / "goal_pause_events.json").read_bytes() == pause_bytes
        assert wired.goal_pause("project").owner_instruction is not None
        assert agent._dispositions.path.read_bytes() == ledger_before
        assert agent._dispositions.status("acp:earlier-uncertain") == "unknown"
        generation = agent._goal_store.snapshot(goal.id)
        assert generation.state == "blocked" and generation.attempt_id is not None
        with pytest.raises(UnresolvedAttempt):
            agent._goal_store.resume(goal.id, generation.number)
        agent._schedule_goal("project")
        assert not agent._pending_turns.get("project")
        diagnostics = list((wired.root / "diagnostics").glob("*.json"))
        assert len(diagnostics) == 1
        diagnostic = json.loads(diagnostics[0].read_text())
        assert diagnostic["outcome"] == "failed; inputs must not be replayed automatically"
        if outcome == "failed_done":
            assert diagnostic["reason"] == "assistant_final_stop_missing"
            assert diagnostic["measurements"] == {"exit_code": 0}
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("attribution", ["model", "runtime", "missing", "stale_owner"])
def test_nonowner_or_stale_pause_does_not_bypass_failure_block(wired, tmp_path, attribution):
    wired.register(Thread(name="project", tags=frozenset(), worktree=str(tmp_path)))
    initial = wired.update_goal("project", "set", text="Work")
    if attribution == "stale_owner":
        wired.update_goal("project", "paused", goal_id=initial.id, owner_action=True)
        wired.update_goal("project", "active", goal_id=initial.id, owner_action=True)
    paused = wired.update_goal(
        "project", "paused", goal_id=initial.id, model_report=attribution == "model"
    )
    if attribution == "missing":
        (wired.root / "goal_pause_events.json").unlink()
    elif attribution == "stale_owner":
        # Keep the earlier owner event but no attribution for the current revision.
        path = wired.root / "goal_pause_events.json"
        events = json.loads(path.read_text())
        del events[f"{initial.id}:{paused.revision}"]
        path.write_text(json.dumps(events))
    blocked = wired.block_goal_after_failed_turn(
        "project", started_goal=initial, expected_worktree=str(tmp_path), diagnostic="Failed"
    )
    assert blocked.status == "blocked"
    assert blocked.revision == paused.revision + 1
    assert blocked.block_reason == "Failed"
