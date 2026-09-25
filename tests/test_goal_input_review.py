"""Explicit inspection decisions unblock waiting without inventing native receipts."""

import pytest

from agent_comms import Thread, wire
from agent_comms.acp import CommsAgent
from agent_comms.tools import TOOLS


async def test_inspected_unknown_dependencies_allow_standby_but_never_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _: None)
    await agent.new_session(str(tmp_path / "worker"))
    comms.register(Thread("parent", frozenset(), str(tmp_path)))
    comms.register(Thread("other", frozenset(), str(tmp_path)))
    goal = comms.update_goal(
        "worker", "set", text="Delegate and wait", owner_store=agent._open_goal_store()
    )
    messages = [
        comms.send_message("parent", "worker", text) for text in ("Set standby", "Yes wait")
    ]
    inbox = next(t for t in TOOLS if t.name == "comms_inbox")
    report = next(t for t in TOOLS if t.name == "comms_goal")
    args = {
        "goal_id": goal.id,
        "status": "standby",
        "progress": "Waiting for later parent reply",
        "wait_for": ["parent"],
    }
    try:
        await agent._drain_inbox("worker")
        with pytest.raises(ValueError, match="pending or UNKNOWN"):
            report.invoke(comms, args)
        result = inbox.invoke(comms, {"thread": "worker"})
        assert result["messages"] == []
        inputs = result["unresolved_inputs"]
        assert [row["sequence"] for row in inputs] == [m.seq for m in messages]
        keys = [row["inputId"] for row in inputs]
        with pytest.raises(ValueError, match="pending or UNKNOWN"):
            report.invoke(comms, {**args, "reviewed_inputs": keys[1:]})
        assert all(not agent._dispositions.get(key).get("goal_reviews") for key in keys)
        with pytest.raises(ValueError, match="recipient"):
            report.invoke(comms, {**args, "reviewed_inputs": ["bus:999999"]})
        unrelated = comms.send_message("other", "worker", "Not a declared dependency")
        await agent._drain_inbox("worker")
        with pytest.raises(ValueError, match="declared dependencies"):
            report.invoke(comms, {**args, "reviewed_inputs": [*keys, f"bus:{unrelated.seq}"]})
        assert (
            report.invoke(comms, {**args, "reviewed_inputs": keys})["goal_execution"]["state"]
            == "standby"
        )
        agent._schedule_goal("worker")
        assert not agent._pending_turns.get("worker")
        for key in keys:
            row = agent._dispositions.get(key)
            assert row["status"] == "unknown" and row["native_id"] is None
            assert agent._dispositions.reviewed_for_goal(row, goal.id)
        # Durable explicit handling survives reopening and a later wait declaration.
        reopened = wire(comms.root)
        reopened.update_goal("worker", "active", goal_id=goal.id)
        reopened.update_goal("worker", "standby", goal_id=goal.id, wait_for=["parent"])
        fresh = comms.send_message("parent", "worker", "New result")
        monkeypatch.setattr(agent, "_schedule_wake", lambda _: None)
        await agent._drain_inbox("worker")
        pending = agent._pending_turns["worker"]
        assert len(pending) == 1 and pending[0].origin.seq == fresh.seq
        # The current owner pause cannot be bypassed by an inspection argument.
        comms.update_goal("worker", "paused", goal_id=goal.id, owner_action=True)
        with pytest.raises(ValueError, match="paused by the owner"):
            report.invoke(comms, {**args, "reviewed_inputs": keys})
        assert comms.registry.require("worker").goal.status == "paused"
    finally:
        await agent.shutdown()
