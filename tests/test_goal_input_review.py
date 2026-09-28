"""Explicit inspection decisions unblock waiting without inventing native receipts."""

import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms.goal_actions import (
    ActiveGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.threads import Thread
from agent_comms.tools import TOOLS


async def test_inspected_unknown_dependencies_allow_standby_but_never_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _: None)
    await agent.new_session(str(tmp_path / "worker"))
    comms.threads.register(Thread("parent", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    comms.agents.begin_turn("parent", "parent-delegation-in-flight")
    comms.threads.register(Thread("other", frozenset(), str(tmp_path)))
    goal = comms.goals.update_goal(
        "worker", SetGoalAction(text="Delegate and wait"), owner_store=agent.turns.open_goal_store()
    )
    messages = [
        comms.messaging.send_message("parent", "worker", text) for text in ("Set standby", "Yes wait")
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
        await agent.inputs.drain_inbox("worker")
        with pytest.raises(ValueError, match="pending or UNKNOWN"):
            report.invoke(comms, args)
        admission = comms.registry.snapshot().admission_generations["worker"]
        agent.inputs.dispositions.record(
            "acp:owner-input",
            seq=None,
            owner="worker",
            admission=admission,
            target="worker",
            text="An uncertain user follow-up",
        )
        result = inbox.invoke(
            comms, {"thread": "worker", "goal_id": goal.id, "wait_for": ["parent"]}
        )
        assert result["messages"] == []
        assert len(result["unresolved_inputs"]) == 3
        review = result["standby_review"]
        assert [row["sequence"] for row in review["messages"]] == [m.seq for m in messages]
        assert review["excluded_inputs"][0]["reason"] == "owner_input_without_bus_sequence"
        keys = review["reviewed_inputs"]
        with pytest.raises(ValueError, match="recipient"):
            report.invoke(
                comms,
                {
                    **args,
                    "reviewed_inputs": [row["inputId"] for row in result["unresolved_inputs"]],
                },
            )
        assert all(not agent.inputs.dispositions.read().rows.get(key).goal_reviews for key in keys)
        assert not agent.inputs.dispositions.read().rows.get("acp:owner-input").goal_reviews
        with pytest.raises(ValueError, match="pending or UNKNOWN"):
            report.invoke(comms, {**args, "reviewed_inputs": keys[1:]})
        assert all(not agent.inputs.dispositions.read().rows.get(key).goal_reviews for key in keys)
        with pytest.raises(ValueError, match="recipient"):
            report.invoke(comms, {**args, "reviewed_inputs": ["bus:999999"]})
        unrelated = comms.messaging.send_message("other", "worker", "Not a declared dependency")
        await agent.inputs.drain_inbox("worker")
        scoped = comms.goals.goal_input_review("worker", goal.id, ["parent"])
        assert scoped["reviewed_inputs"] == keys
        assert len(scoped["excluded_inputs"]) == 2
        with pytest.raises(ValueError, match="declared dependencies"):
            report.invoke(comms, {**args, "reviewed_inputs": [*keys, f"bus:{unrelated.seq}"]})
        assert (
            report.invoke(comms, {**args, "reviewed_inputs": keys})["goal_execution"]["state"]
            == "standby"
        )
        # A benign standby transition must not strand unattempted NEW direct
        # DMs: they remain typed ordinary interrupts (proven fresh by their
        # unattempted dispositions) and dispatch later, never as replayed
        # dependency replies.
        pending = agent.inputs.pending_turns.get("worker", [])
        assert pending and all(turn.direct_interrupt_goal_id == goal.id for turn in pending)
        assert all(turn.goal_wait_id is None for turn in pending)
        for turn in pending:
            row = agent.inputs.dispositions.read().rows.get(turn.direct_interrupt_input_key)
            assert row is not None and row.declared_name == "unknown" and row.native_id is None
        agent.turns.schedule_goal("worker")
        # The standby wait still defers any goal turn; the ordinary interrupts
        # stay queued and unattempted.
        assert agent.inputs.pending_turns.get("worker")
        again = comms.goals.goal_input_review("worker", goal.id, ["parent"])
        assert again["reviewed_inputs"] == []
        assert [row["inputId"] for row in again["already_reviewed_inputs"]] == keys
        for key in keys:
            row = agent.inputs.dispositions.read().rows.get(key)
            assert row.declared_name == "unknown" and row.native_id is None
            assert row.reviewed_for_goal(goal.id)
        # Durable explicit handling survives reopening and a later wait declaration.
        reopened = wire(comms.root)
        reopened.goals.update_goal("worker", ActiveGoalAction(expect=GoalPrecondition(goal_id=goal.id)))
        reopened.goals.update_goal(
            "worker",
            StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("parent",)),
        )
        fresh = comms.messaging.send_message("parent", "worker", "New result")
        monkeypatch.setattr(agent.inputs, "schedule_wake", lambda _: None)
        await agent.inputs.drain_inbox("worker")
        pending = agent.inputs.pending_turns["worker"]
        fresh_entries = [turn for turn in pending if turn.origin.seq == fresh.seq]
        assert len(fresh_entries) == 1
        # After standby the same sender is a declared dependency: this fresh
        # DM is a typed goal-wake continuation, NOT an ordinary interrupt.
        assert fresh_entries[0].goal_wait_id is not None
        assert fresh_entries[0].direct_interrupt_goal_id is None
        assert all(
            turn.direct_interrupt_goal_id == goal.id
            for turn in pending
            if turn.origin.seq != fresh.seq
        )
        # The current owner pause cannot be bypassed by an inspection argument.
        comms.goals.update_goal(
            "worker",
            PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)),
            actor=OwnerInvocable,
        )
        with pytest.raises(ValueError, match="paused by the owner"):
            report.invoke(comms, {**args, "reviewed_inputs": keys})
        assert comms.registry.require("worker").goal.state.declared_name == "paused"
    finally:
        await agent.shutdown()
