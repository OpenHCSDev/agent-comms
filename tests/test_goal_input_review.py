"""Canonical inspection unblocks standby without fabricating native delivery."""

from agent_comms.lifecycle_capability import Unresolved
from agent_comms.channel_targets import CommandTarget
import os

import pytest
from conftest import publish_initial

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from conftest import installed_comms
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.goal_presentation import GoalExecution
from agent_comms.goal_actions import (
    ActiveGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.input_disposition import InputDispositions
from agent_comms.tools import CommsInboxTool
from agent_comms.tool_request import ToolRequest
from agent_comms.tool_caller import ThreadCaller
from agent_comms.read_basis import NamedViewer
from stored_inputs import stored_inputs
from agent_comms.threads import Thread


def test_inspected_canonical_dependencies_allow_standby_without_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_THREAD", "worker")
    comms = wire(tmp_path / "wire")
    for name in ("worker", "parent", "other"):
        comms.registry.declare(
            Thread(name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
    comms.agents.begin_turn("parent", "parent-delegation-in-flight")
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Delegate and wait"))
    messages = [
        publish_initial(comms, "parent", "worker", text)
        for text in ("Set standby", "Yes wait")
    ]
    ledger = InputDispositions(comms.root)
    claim_scope = comms.registry.snapshot().claim_generation("worker")
    ledger.record(
        "acp:owner-input",
        seq=None,
        owner="worker",
        claim_scope=claim_scope,
        target="worker",
        text="An uncertain user follow-up",
    )
    inbox = next(
        t for t in ToolRequest.members_with(ToolRequest) if t.declared_name == "comms_inbox"
    )
    report = next(
        t for t in ToolRequest.members_with(ToolRequest) if t.declared_name == "comms_goal"
    )
    args = {
        "goal_id": goal.id,
        "status": "standby",
        "progress": "Waiting for later reply",
        "wait_for": ["parent"],
    }
    with pytest.raises(ValueError):
        report.invoke(comms, args, caller=ThreadCaller("worker"))
    before = ledger.path.read_bytes()
    messages = comms.bus.inbox("worker").page.messages
    comms.messaging.acknowledge("worker", tuple(message.reference for message in messages))
    result = inbox.invoke(comms, {"thread": "worker", "standby": {"goal_id": goal.id, "wait_for": ["parent"]}}, caller=ThreadCaller("worker"))
    assert result["messages"] == []  # Display ACK is not handling evidence.
    review = result["standby_review"]
    assert [row["sequence"] for row in review["messages"]] == [m.seq for m in messages]
    assert review["excluded_inputs"][0]["reason"] == "owner_input_without_bus_sequence"
    assert ledger.path.read_bytes() == before  # Inspection cannot adopt or schedule inputs.
    keys = review["reviewed_inputs"]
    for invalid in (["acp:owner-input"], ["bus:999999"]):
        with pytest.raises(ValueError):
            report.invoke(comms, {**args, "reviewed_inputs": invalid}, caller=ThreadCaller("worker"))
    with pytest.raises(ValueError):
        report.invoke(comms, {**args, "reviewed_inputs": keys[1:]}, caller=ThreadCaller("worker"))
    assert ledger.path.read_bytes() == before  # Failed partial review commits nothing.
    unrelated = comms.messaging.send("other", "worker", "Not a declared dependency").message
    ledger.record(
        f"bus:{unrelated.seq}",
        seq=unrelated.seq,
        owner="worker",
        claim_scope=claim_scope,
        target="worker",
        text=unrelated.body,
    )
    scoped = comms.goals.goal_input_review("worker", goal.id, ["parent"])
    assert list(scoped.reviewed_inputs) == keys and len(scoped.excluded_inputs) == 2
    with pytest.raises(ValueError):
        report.invoke(comms, {**args, "reviewed_inputs": [*keys, f"bus:{unrelated.seq}"]}, caller=ThreadCaller("worker"))
    result = report.invoke(comms, {**args, "reviewed_inputs": keys}, caller=ThreadCaller("worker"))
    assert FieldCodec.decode(GoalExecution, result["goal_execution"]).standby()
    again = comms.goals.goal_input_review("worker", goal.id, ["parent"])
    assert again.reviewed_inputs == ()
    assert [row.input_id for row in again.already_reviewed_inputs] == keys
    for message, key in zip(messages, keys, strict=True):
        row = stored_inputs(ledger.root).rows[key]
        row.require(Unresolved, ValueError("review required"))
        assert row.accepts_reservation
        assert not hasattr(row, "native_id") and not hasattr(row, "turn_id")
        assert row.source_text == message.body and not hasattr(row, "sent_text")
        assert row.claim_generation == claim_scope and row.reviewed_for_goal(goal.id)
    assert not stored_inputs(ledger.root).rows["acp:owner-input"].goal_reviews
    generation = comms.goals.open_goal_store().snapshot(goal.id)
    assert generation is not None and generation.lifecycle.ready
    assert generation.attempt_id is None
    reopened = wire(comms.root)
    reopened.goals.update_goal("worker", ActiveGoalAction(expect=GoalPrecondition(goal_id=goal.id)))
    reopened.goals.update_goal(
        "worker", StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("parent",))
    )
    fresh = comms.messaging.send("parent", "worker", "New result").message
    assert reopened.goals.goal_input_review("worker", goal.id, ["parent"]).reviewed_inputs == (
        f"bus:{fresh.seq}",
    )
    comms.goals.update_goal(
        "worker", PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)), actor=OwnerInvocable
    )
    with pytest.raises(ValueError):
        report.invoke(comms, {**args, "reviewed_inputs": keys}, caller=ThreadCaller("worker"))
    assert comms.registry.require("worker").goal.state.declared_name == "paused"


@pytest.mark.parametrize("rebound", ["worker", "parent"])
def test_standby_excludes_retained_rebound_history_but_retains_rename(tmp_path, rebound):
    comms = installed_comms(tmp_path)
    for name in ("worker", "parent"):
        comms.registry.declare(
            Thread(name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
    old = comms.messaging.send("parent", "worker", "Old incarnation").message
    comms.registry.unregister(rebound)
    comms.registry.remove(rebound)
    comms.registry.declare(
        Thread(rebound, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    comms.agents.begin_turn("parent", "new-turn")
    goal = comms.goals.update_goal("worker", SetGoalAction(text="Current goal"))
    assert comms.goals.goal_input_review("worker", goal.id, ["parent"]).reviewed_inputs == ()
    action = StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("parent",))
    comms.goals.update_goal("worker", action)
    current = comms.messaging.send("parent", "worker", "Current incarnation").message
    comms.registry.rename("parent", "renamed-parent")
    review = comms.goals.goal_input_review("worker", goal.id, ["renamed-parent"])
    assert review.reviewed_inputs == (f"bus:{current.seq}",)
    assert old in list(comms.views.history_page(CommandTarget.named("worker"), viewer=NamedViewer("renamed-parent")).messages)


def test_inbox_standby_is_one_record(wired, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_THREAD", "fixer")
    goal = wired.goals.update_goal("fixer", SetGoalAction(text="Wait for the parent"))
    wired.messaging.send("PR111", "fixer", "Reply").message
    standby = {"goal_id": goal.id, "wait_for": ["PR111"]}
    for half in ({"goal_id": goal.id}, {"wait_for": ["PR111"]}):
        with pytest.raises(MalformedRecord):
            CommsInboxTool.invoke(wired, {"thread": "fixer", "standby": half}, caller=ThreadCaller("fixer"))
    result = CommsInboxTool.invoke(wired, {"thread": "fixer", "standby": standby}, caller=ThreadCaller("fixer"))
    assert len(result["messages"]) == 1  # A refused half record read nothing.
    assert (result["standby_review"]["goal_id"], result["standby_review"]["wait_for"]) == (goal.id, ["PR111"])
