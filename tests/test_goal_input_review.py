"""Canonical inspection unblocks standby without fabricating native delivery."""

import os

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.goal_actions import (
    ActiveGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.input_disposition import InputDispositions
from agent_comms.threads import Thread
from agent_comms.tools import TOOLS


def test_inspected_canonical_dependencies_allow_standby_without_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AGENT_ID", "worker")
    comms = wire(tmp_path / "wire")
    for name in ("worker", "parent", "other"):
        comms.threads.register(
            Thread(
                name,
                frozenset(),
                str(tmp_path),
                process_identity=ProcessIdentity.capture(os.getpid()),
            )
        )
    comms.agents.begin_turn("parent", "parent-delegation-in-flight")
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Delegate and wait"))
    messages = [
        comms.messaging.send_message("parent", "worker", text)
        for text in ("Set standby", "Yes wait")
    ]
    ledger = InputDispositions(comms.root / InputDispositions.filename)
    admission = comms.registry.snapshot().admission_generations["worker"]
    ledger.record(
        "acp:owner-input",
        seq=None,
        owner="worker",
        admission=admission,
        target="worker",
        text="An uncertain user follow-up",
    )
    inbox = next(t for t in TOOLS if t.name == "comms_inbox")
    report = next(t for t in TOOLS if t.name == "comms_goal")
    args = {
        "goal_id": goal.id,
        "status": "standby",
        "progress": "Waiting for later reply",
        "wait_for": ["parent"],
    }
    with pytest.raises(ValueError, match="pending or UNKNOWN"):
        report.invoke(comms, args)
    before = ledger.path.read_bytes()
    comms.messaging.acknowledge("worker")
    result = inbox.invoke(comms, {"thread": "worker", "goal_id": goal.id, "wait_for": ["parent"]})
    assert result["messages"] == []  # Display ACK is not handling evidence.
    review = result["standby_review"]
    assert [row["sequence"] for row in review["messages"]] == [m.seq for m in messages]
    assert review["excluded_inputs"][0]["reason"] == "owner_input_without_bus_sequence"
    assert ledger.path.read_bytes() == before  # Inspection cannot adopt or schedule inputs.
    keys = review["reviewed_inputs"]
    for invalid in (["acp:owner-input"], ["bus:999999"]):
        with pytest.raises(ValueError, match="recipient"):
            report.invoke(comms, {**args, "reviewed_inputs": invalid})
    with pytest.raises(ValueError, match="pending or UNKNOWN"):
        report.invoke(comms, {**args, "reviewed_inputs": keys[1:]})
    assert ledger.path.read_bytes() == before  # Failed partial review commits nothing.
    unrelated = comms.messaging.send_message("other", "worker", "Not a declared dependency")
    ledger.record(
        f"bus:{unrelated.seq}",
        seq=unrelated.seq,
        owner="worker",
        admission=admission,
        target="worker",
        text=unrelated.body,
    )
    scoped = comms.goals.goal_input_review("worker", goal.id, ["parent"])
    assert scoped["reviewed_inputs"] == keys and len(scoped["excluded_inputs"]) == 2
    with pytest.raises(ValueError, match="declared dependencies"):
        report.invoke(comms, {**args, "reviewed_inputs": [*keys, f"bus:{unrelated.seq}"]})
    assert (
        report.invoke(comms, {**args, "reviewed_inputs": keys})["goal_execution"]["state"]
        == "standby"
    )
    again = comms.goals.goal_input_review("worker", goal.id, ["parent"])
    assert again["reviewed_inputs"] == []
    assert [row["inputId"] for row in again["already_reviewed_inputs"]] == keys
    for message, key in zip(messages, keys, strict=True):
        row = ledger.read().rows[key]
        assert row.unresolved and row.native_id is None and row.turn_id is None
        assert row.source_text == message.body and row.sent_text is None
        assert row.admission == admission and row.reviewed_for_goal(goal.id)
    assert not ledger.read().rows["acp:owner-input"].goal_reviews
    assert not (comms.root / "goal-private").exists()
    reopened = wire(comms.root)
    reopened.goals.update_goal("worker", ActiveGoalAction(expect=GoalPrecondition(goal_id=goal.id)))
    reopened.goals.update_goal(
        "worker", StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("parent",))
    )
    fresh = comms.messaging.send_message("parent", "worker", "New result")
    assert reopened.goals.goal_input_review("worker", goal.id, ["parent"])["reviewed_inputs"] == [
        f"bus:{fresh.seq}"
    ]
    comms.goals.update_goal(
        "worker", PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)), actor=OwnerInvocable
    )
    with pytest.raises(ValueError, match="paused by the owner"):
        report.invoke(comms, {**args, "reviewed_inputs": keys})
    assert comms.registry.require("worker").goal.state.declared_name == "paused"


@pytest.mark.parametrize("rebound", ["worker", "parent"])
def test_standby_excludes_retained_rebound_history_but_retains_rename(tmp_path, rebound):
    comms = wire(tmp_path)
    for name in ("worker", "parent"):
        comms.threads.register(
            Thread(
                name,
                frozenset(),
                str(tmp_path),
                process_identity=ProcessIdentity.capture(os.getpid()),
            )
        )
    old = comms.messaging.send_message("parent", "worker", "Old incarnation")
    comms.registry.unregister(rebound)
    comms.registry.remove(rebound)
    comms.threads.register(
        Thread(
            rebound,
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    comms.agents.begin_turn("parent", "new-turn")
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Current goal"))
    assert comms.goals.goal_input_review("worker", goal.id, ["parent"])["reviewed_inputs"] == []
    action = StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("parent",))
    comms.goals.update_goal("worker", action)
    current = comms.messaging.send_message("parent", "worker", "Current incarnation")
    comms.registry.rename("parent", "renamed-parent")
    review = comms.goals.goal_input_review("worker", goal.id, ["renamed-parent"])
    assert review["reviewed_inputs"] == [f"bus:{current.seq}"]
    assert old in comms.bus.dm_history("renamed-parent", "worker")
