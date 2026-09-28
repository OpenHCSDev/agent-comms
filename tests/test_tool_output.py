"""Oversized inbox output remains inspectable without acknowledging unseen messages."""

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.goal_actions import SetGoalAction
from agent_comms.input_disposition import InputDispositions
from agent_comms.threads import Thread
from agent_comms.tool_output import MAX_INLINE_OUTPUT_BYTES, materialize_oversized_output
from agent_comms.tools import invoke_tool


def seed_unknown(comms, count, text):
    dispositions = InputDispositions(comms.root / InputDispositions.filename)
    rows = {
        f"bus:{index}": {
            "key": f"bus:{index}",
            "sequence": index,
            "owner": "b",
            "admission": index,
            "target": "b",
            "source_text": f"{index}: {text}",
            "turn_id": "private-turn",
            "native_id": "f" * 32,
            "sent_text": "private-native-prompt",
            "kind": "bound_unknown",
        }
        for index in range(1, count + 1)
    }
    dispositions.path.write_text(json.dumps({"version": 1, "rows": rows}))
    return dispositions


@pytest.fixture
def inbox_comms(comms):
    for name in ("a", "b"):
        comms.threads.register(Thread(name=name, tags=frozenset(), worktree="/wt"))
    return comms


@pytest.mark.parametrize("count,text", [(930, "historical input " * 65), (1, "🧪漢" * 40_000)])
def test_large_inbox_is_bounded_lossless_private_and_does_not_ack(inbox_comms, count, text):
    comms = inbox_comms
    ledger = seed_unknown(comms, count, text)
    comms.messaging.send("a", "b", "An unread new message")
    authority_before = ledger.path.read_bytes()
    expected = {
        "messages": [message.to_wire() for message in comms.bus.inbox("b")],
        "acknowledged": 0,
        "unresolved_inputs": comms.goals.unresolved_inputs("b"),
    }

    result = invoke_tool(comms, "comms_inbox", {"thread": "b"})

    assert len(json.dumps(result, indent=2).encode()) < MAX_INLINE_OUTPUT_BYTES
    assert result["complete"] is False
    assert result["ackDeferred"] is True
    assert result["acknowledged"] == 0
    assert result["counts"] == {"messages": 1, "unresolved_inputs": count}
    assert result["messages"] == result["unresolved_inputs"] == []
    assert "omitted" in result["instruction"]
    assert "selectively" in result["instruction"]
    assert comms.bus.pending_count("b") == 1
    assert ledger.path.read_bytes() == authority_before
    artifact = Path(result["result_file"])
    assert artifact.parent == comms.root / "tool-output"
    assert json.loads(artifact.read_text()) == expected
    assert artifact.stem == hashlib.sha256(artifact.read_bytes()).hexdigest()
    if os.name == "posix":
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
        assert stat.S_IMODE(artifact.parent.stat().st_mode) == 0o700
    assert "private-native-prompt" not in artifact.read_text()
    assert "native_id" not in artifact.read_text()
    snapshot_stat = artifact.stat()
    assert invoke_tool(comms, "comms_inbox", {"thread": "b"}) == result
    assert artifact.stat().st_ino == snapshot_stat.st_ino
    assert artifact.stat().st_mtime_ns == snapshot_stat.st_mtime_ns


@pytest.mark.parametrize("ack", [False, True])
def test_small_inbox_keeps_existing_contract(inbox_comms, ack):
    comms = inbox_comms
    ledger = seed_unknown(comms, 1, "Earlier unresolved message")
    comms.messaging.send("a", "b", "hello")
    expected = {
        "messages": [message.to_wire() for message in comms.bus.inbox("b")],
        "acknowledged": int(ack),
        "unresolved_inputs": comms.goals.unresolved_inputs("b"),
    }
    authority_before = ledger.path.read_bytes()
    assert invoke_tool(comms, "comms_inbox", {"thread": "b", "ack": ack}) == expected
    assert comms.bus.pending_count("b") == int(not ack)
    assert ledger.path.read_bytes() == authority_before
    assert not (comms.root / "tool-output").exists()


def test_publication_failure_precedes_ack(inbox_comms, monkeypatch):
    comms = inbox_comms
    ledger = seed_unknown(comms, 1, "body" * 20_000)
    comms.messaging.send("a", "b", "Still unread after failure")
    authority_before = ledger.path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("artifact publication failed")

    monkeypatch.setattr("agent_comms.tool_output._atomic_write_text", fail)
    with pytest.raises(OSError, match="artifact publication failed"):
        invoke_tool(comms, "comms_inbox", {"thread": "b"})
    assert comms.bus.pending_count("b") == 1
    assert ledger.path.read_bytes() == authority_before


@pytest.mark.parametrize("large_body", ["large relevant reply " * 5_000, "🧪" * 3_000])
def test_oversized_standby_exposes_counts_without_unseen_review_keys(
    inbox_comms, monkeypatch, large_body
):
    comms = inbox_comms
    monkeypatch.setenv("PI_AGENT_ID", "b")
    goal = comms.goals.update_goal(
        "b", SetGoalAction(text="Wait for a after reviewing its replies")
    )
    messages = [
        comms.messaging.send_message("a", "b", body)
        for body in (large_body, "Previously reviewed reply")
    ]
    dispositions = InputDispositions(comms.root / InputDispositions.filename)
    for message in messages:
        dispositions.record(
            f"bus:{message.seq}",
            seq=message.seq,
            owner="b",
            admission=1,
            target="b",
            text=message.body,
        )
    dispositions.record(
        "acp:owner-input",
        seq=None,
        owner="b",
        admission=1,
        target="b",
        text="Uncertain owner follow-up",
    )
    dispositions.review_for_goal(
        (f"bus:{messages[1].seq}",),
        owners=frozenset({"b"}),
        goal_id=goal.id,
        goal_revision=goal.revision,
        turn_id="earlier-review",
    )
    review = comms.goals.goal_input_review("b", goal.id, ["a"])
    before = dispositions.path.read_bytes()
    result = invoke_tool(
        comms,
        "comms_inbox",
        {"thread": "b", "ack": False, "goal_id": goal.id, "wait_for": ["a"]},
    )
    assert result["standby_review"] == {
        "complete": False,
        "counts": {"messages": 1, "already_reviewed_inputs": 1, "excluded_inputs": 1},
    }
    assert result["ackDeferred"] is False
    assert dispositions.path.read_bytes() == before
    report = {
        "goal_id": goal.id,
        "status": "standby",
        "progress": "Waiting for a",
        "wait_for": ["a"],
    }
    with pytest.raises(ValueError, match="pending or UNKNOWN"):
        invoke_tool(comms, "comms_goal", report)
    assert dispositions.path.read_bytes() == before
    full_review = json.loads(Path(result["result_file"]).read_text())["standby_review"]
    assert full_review == review
    assert full_review["messages"][0]["text"] == messages[0].body
    comms.registry.register(
        replace(comms.registry.require("a"), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    comms.agents.begin_turn("a", "next-a-result-in-flight")
    invoke_tool(comms, "comms_goal", {**report, "reviewed_inputs": full_review["reviewed_inputs"]})
    assert comms.goals.goal_wait("b") is not None
    assert all(
        dispositions.read().rows[f"bus:{message.seq}"].declared_name == "reserved"
        for message in messages
    )
    assert dispositions.read().rows["acp:owner-input"].declared_name == "reserved"
    assert not dispositions.read().rows.get("acp:owner-input").goal_reviews


def test_materialization_measures_escaped_transport_bytes(tmp_path):
    response = {"text": "🧪" * 3_000}
    assert len(json.dumps(response, ensure_ascii=False).encode()) < MAX_INLINE_OUTPUT_BYTES
    assert len(json.dumps(response, indent=2).encode()) > MAX_INLINE_OUTPUT_BYTES
    artifact = materialize_oversized_output(tmp_path, response)
    assert artifact is not None
    assert json.loads(artifact.read_text()) == response


def test_cached_materialization_restores_private_modes_and_corrupt_content(tmp_path):
    response = {"text": "public result " * 3_000}
    artifact = materialize_oversized_output(tmp_path, response)
    artifact.chmod(0o644)
    artifact.parent.chmod(0o755)
    assert materialize_oversized_output(tmp_path, response) == artifact
    if os.name == "posix":
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
        assert stat.S_IMODE(artifact.parent.stat().st_mode) == 0o700
    artifact.write_text("accidentally changed snapshot")
    assert materialize_oversized_output(tmp_path, response) == artifact
    assert json.loads(artifact.read_text()) == response


def test_small_dependency_review_stays_inline_despite_large_excluded_history(
    inbox_comms, monkeypatch
):
    comms = inbox_comms
    monkeypatch.setenv("PI_AGENT_ID", "b")
    # The reviewed standby liveness gate refuses a declared dependency with
    # no active turn. Give "a" a live in-process turn for this fixture only.
    comms.threads.register(
        replace(comms.registry.require("a"), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    comms.agents.begin_turn("a", "a-review-in-flight")
    goal = comms.goals.update_goal("b", SetGoalAction(text="Review a and wait for its next reply"))
    dispositions = InputDispositions(comms.root / InputDispositions.filename)
    for index in range(930):
        dispositions.record(
            f"acp:owner-{index}",
            seq=None,
            owner="b",
            admission=1,
            target="b",
            text="Uncertain owner input " * 45,
        )
    messages = [
        comms.messaging.send_message("a", "b", body)
        for body in ("Already reviewed", "Complete new reply: " + "x" * 548)
    ]
    for message in messages:
        dispositions.record(
            f"bus:{message.seq}",
            seq=message.seq,
            owner="b",
            admission=1,
            target="b",
            text=message.body,
        )
    dispositions.review_for_goal(
        (f"bus:{messages[0].seq}",),
        owners=frozenset({"b"}),
        goal_id=goal.id,
        goal_revision=goal.revision,
        turn_id="previous-review",
    )
    before = dispositions.path.read_bytes()
    result = invoke_tool(
        comms, "comms_inbox", {"thread": "b", "goal_id": goal.id, "wait_for": ["a"]}
    )
    review = result["standby_review"]
    assert [row["text"] for row in review["messages"]] == [messages[1].body]
    assert review["reviewed_inputs"] == [f"bus:{messages[1].seq}"]
    assert review["messages_complete"] is True and review["complete"] is False
    assert review["goal_id"] == goal.id and review["wait_for"] == ["a"]
    assert review["counts"] == {
        "messages": 1,
        "already_reviewed_inputs": 1,
        "excluded_inputs": 930,
    }
    assert "excluded_inputs" not in review and "already_reviewed_inputs" not in review
    assert len(json.dumps(result, indent=2).encode()) <= MAX_INLINE_OUTPUT_BYTES
    assert result["complete"] is False and result["ackDeferred"] is True
    assert result["acknowledged"] == 0 and comms.bus.pending_count("b") == 2
    assert dispositions.path.read_bytes() == before
    full = json.loads(Path(result["result_file"]).read_text())
    assert full["standby_review"] == comms.goals.goal_input_review("b", goal.id, ["a"])
    assert len(full["standby_review"]["excluded_inputs"]) == 930
    report = {
        "goal_id": goal.id,
        "status": "standby",
        "progress": "Wait for next reply",
        "wait_for": ["a"],
        "reviewed_inputs": review["reviewed_inputs"],
    }
    with pytest.raises(ValueError, match="recipient"):
        invoke_tool(comms, "comms_goal", {**report, "reviewed_inputs": ["acp:owner-0"]})
    assert dispositions.path.read_bytes() == before
    # A reply arriving after this snapshot still blocks standby atomically.
    late = comms.messaging.send_message("a", "b", "New evidence after inbox inspection")
    dispositions.record(
        f"bus:{late.seq}", seq=late.seq, owner="b", admission=1, target="b", text=late.body
    )
    after_arrival = dispositions.path.read_bytes()
    with pytest.raises(ValueError, match="pending or UNKNOWN"):
        invoke_tool(comms, "comms_goal", report)
    assert dispositions.path.read_bytes() == after_arrival
    fresh = invoke_tool(
        comms, "comms_inbox", {"thread": "b", "goal_id": goal.id, "wait_for": ["a"]}
    )["standby_review"]
    assert fresh["reviewed_inputs"] == [f"bus:{messages[1].seq}", f"bus:{late.seq}"]
    invoke_tool(comms, "comms_goal", {**report, "reviewed_inputs": fresh["reviewed_inputs"]})
    assert comms.goals.goal_wait("b") is not None
    assert dispositions.read().rows[f"bus:{messages[1].seq}"].declared_name == "reserved"
    assert not dispositions.read().rows.get("acp:owner-0").goal_reviews
