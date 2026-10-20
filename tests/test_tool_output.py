"""Inbox pages drain oldest first without changing uncertain inputs."""

import asyncio
import json
import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.goal_actions import SetGoalAction
from agent_comms.input_attempt import BoundUnknownInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.threads import Thread
from agent_comms.tool_output import MAX_INLINE_OUTPUT_BYTES
from agent_comms.message_page import MessageReadRequest, PageBounds
from agent_comms.read_basis import ExplicitWatchAcknowledgment
from agent_comms.tools import invoke_tool
from agent_comms.tool_caller import ThreadCaller, NoThreadCaller
from agent_comms.field_codec import FieldCodec
from agent_comms.text_digest import TextDigest
from stored_inputs import insert_inputs, stored_inputs
from agent_comms.goal_waits import RecordedGoalWait


def seed_unknown(comms, count, text):
    dispositions = InputDispositions(comms.root)
    rows = {
        f"bus:{index}": FieldCodec.encode(BoundUnknownInput(
            key=f"bus:{index}", sequence=index, owner="b", claim_scope=index,
            target="b", source_text=f"{index}: {text}", turn_id="private-turn",
            native_id="f" * 32, sent_digest=TextDigest.of("private-native-prompt"),
        ))
        for index in range(1, count + 1)
    }
    insert_inputs(comms.root, rows)
    return dispositions


@pytest.fixture
def inbox_comms(root):
    from goal_owner_fixture import canonical_goal_wire

    comms = canonical_goal_wire(root)
    for name in ("a", "b"):
        comms.registry.declare(comms.registry.new_thread(name=name, tags=frozenset(), worktree='/wt'))
    return comms


@pytest.mark.parametrize("count,text", [(930, "historical input " * 65), (1, "🧪漢" * 40_000)])
def test_large_uncertain_inputs_do_not_stop_the_message_page_draining(inbox_comms, count, text):
    comms = inbox_comms
    ledger = seed_unknown(comms, count, text)
    comms.messaging.send("a", "b", "An unread new message").message.message_id
    authority_before = ledger.path.read_bytes()
    expected = {
        "messages": [message.to_wire() for message in comms.bus.inbox("b")],
        "acknowledged": 1,
        "remaining": 0,
        "unresolved_inputs": FieldCodec.encode(comms.goals.unresolved_inputs("b")),
    }

    result = invoke_tool(comms, "comms_inbox", {"thread": "b"}, caller=NoThreadCaller())

    assert result == expected
    assert comms.bus.pending_count("b") == 0
    assert ledger.path.read_bytes() == authority_before
    assert not (comms.root / "tool-output").exists()
    assert "private-native-prompt" not in json.dumps(result)
    assert "native_id" not in json.dumps(result)


@pytest.mark.parametrize("ack", [False, True])
def test_small_inbox_keeps_existing_contract(inbox_comms, ack):
    comms = inbox_comms
    ledger = seed_unknown(comms, 1, "Earlier unresolved message")
    comms.messaging.send("a", "b", "hello").message.message_id
    expected = {
        "messages": [message.to_wire() for message in comms.bus.inbox("b")],
        "acknowledged": int(ack),
        "remaining": int(not ack),
        "unresolved_inputs": FieldCodec.encode(comms.goals.unresolved_inputs("b")),
    }
    authority_before = ledger.path.read_bytes()
    assert invoke_tool(comms, "comms_inbox", {"thread": "b", "ack": ack}, caller=NoThreadCaller()) == expected
    assert comms.bus.pending_count("b") == int(not ack)
    assert ledger.path.read_bytes() == authority_before
    assert not (comms.root / "tool-output").exists()


@pytest.mark.parametrize("limit", [0, -1])
def test_invalid_page_limit_precedes_ack(inbox_comms, limit):
    comms = inbox_comms
    ledger = seed_unknown(comms, 1, "body" * 20_000)
    comms.messaging.send("a", "b", "Still unread after failure").message.message_id
    authority_before = ledger.path.read_bytes()

    with pytest.raises(ValueError, match="limit must be positive"):
        invoke_tool(comms, "comms_inbox", {"thread": "b", "limit": limit}, caller=NoThreadCaller())
    assert comms.bus.pending_count("b") == 1
    assert ledger.path.read_bytes() == authority_before


@pytest.mark.parametrize("large_body", ["large relevant reply " * 5_000, "🧪" * 3_000])
def test_large_standby_review_returns_complete_messages_without_files(
    inbox_comms, monkeypatch, large_body
):
    comms = inbox_comms
    monkeypatch.setenv("AGENT_COMMS_THREAD", "b")
    goal = comms.goals.update_goal(
        "b", SetGoalAction(text="Wait for a after reviewing its replies")
    )
    messages = [
        comms.messaging.send("a", "b", body).message
        for body in (large_body, "Previously reviewed reply")
    ]
    dispositions = InputDispositions(comms.root)
    for message in messages:
        dispositions.record(
            f"bus:{message.seq}",
            seq=message.seq,
            owner="b",
            claim_scope=1,
            target="b",
            text=message.body,
        )
    dispositions.record(
        "acp:owner-input",
        seq=None,
        owner="b",
        claim_scope=1,
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
    before = stored_inputs(dispositions.root)
    result = invoke_tool(
        comms,
        "comms_inbox",
        {"thread": "b", "ack": False, "standby": {"goal_id": goal.id, "wait_for": ["a"]}}, caller=NoThreadCaller(),
    )
    assert result["standby_review"] == FieldCodec.encode(review)
    assert result["acknowledged"] == 0
    assert result["remaining"] == 2
    assert not (comms.root / "tool-output").exists()
    assert stored_inputs(dispositions.root) == before
    report = {
        "goal_id": goal.id,
        "status": "standby",
        "progress": "Waiting for a",
        "wait_for": ["a"],
    }
    with pytest.raises(ValueError, match="pending or UNKNOWN"):
        invoke_tool(comms, "comms_goal", report, caller=ThreadCaller("b"))
    assert stored_inputs(dispositions.root) == before
    full_review = result["standby_review"]
    assert full_review == FieldCodec.encode(review)
    assert full_review["messages"][0]["text"] == messages[0].body
    comms.registry.register(
        replace(comms.registry.require("a"), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    comms.agents.begin_turn("a", "next-a-result-in-flight")
    invoke_tool(comms, "comms_goal", {**report, "reviewed_inputs": full_review["reviewed_inputs"]}, caller=ThreadCaller("b"))
    assert isinstance(comms.goals.goal_wait("b"), RecordedGoalWait)
    assert all(
        stored_inputs(dispositions.root).rows[f"bus:{message.seq}"].declared_name == "reserved"
        for message in messages
    )
    assert stored_inputs(dispositions.root).rows["acp:owner-input"].declared_name == "reserved"
    assert not stored_inputs(dispositions.root).rows.get("acp:owner-input").goal_reviews


def test_dependency_review_and_ack_survive_large_excluded_history(
    inbox_comms, monkeypatch
):
    comms = inbox_comms
    monkeypatch.setenv("AGENT_COMMS_THREAD", "b")
    # The reviewed standby liveness gate refuses a declared dependency with
    # no active turn. Give "a" a live in-process turn for this fixture only.
    comms.registry.declare(
        replace(comms.registry.require("a"), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    comms.agents.begin_turn("a", "a-review-in-flight")
    goal = comms.goals.update_goal("b", SetGoalAction(text="Review a and wait for its next reply"))
    dispositions = InputDispositions(comms.root)
    for index in range(930):
        dispositions.record(
            f"acp:owner-{index}",
            seq=None,
            owner="b",
            claim_scope=1,
            target="b",
            text="Uncertain owner input " * 45,
        )
    messages = [
        comms.messaging.send("a", "b", body).message
        for body in ("Already reviewed", "Complete new reply: " + "x" * 548)
    ]
    for message in messages:
        dispositions.record(
            f"bus:{message.seq}",
            seq=message.seq,
            owner="b",
            claim_scope=1,
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
    before = stored_inputs(dispositions.root)
    result = invoke_tool(
        comms, "comms_inbox", {"thread": "b", "standby": {"goal_id": goal.id, "wait_for": ["a"]}}, caller=NoThreadCaller()
    )
    review = result["standby_review"]
    assert [row["text"] for row in review["messages"]] == [messages[1].body]
    assert review["reviewed_inputs"] == [f"bus:{messages[1].seq}"]
    assert review["goal_id"] == goal.id and review["wait_for"] == ["a"]
    assert len(review["already_reviewed_inputs"]) == 1
    assert len(review["excluded_inputs"]) == 930
    assert result["acknowledged"] == 2 and comms.bus.pending_count("b") == 0
    assert result["remaining"] == 0
    assert stored_inputs(dispositions.root) == before
    assert review == FieldCodec.encode(comms.goals.goal_input_review("b", goal.id, ["a"]))
    assert not (comms.root / "tool-output").exists()
    report = {
        "goal_id": goal.id,
        "status": "standby",
        "progress": "Wait for next reply",
        "wait_for": ["a"],
        "reviewed_inputs": review["reviewed_inputs"],
    }
    with pytest.raises(ValueError, match="recipient"):
        invoke_tool(comms, "comms_goal", {**report, "reviewed_inputs": ["acp:owner-0"]}, caller=ThreadCaller("b"))
    assert stored_inputs(dispositions.root) == before
    # A reply arriving after this snapshot still blocks standby atomically.
    late = comms.messaging.send("a", "b", "New evidence after inbox inspection").message
    dispositions.record(
        f"bus:{late.seq}", seq=late.seq, owner="b", claim_scope=1, target="b", text=late.body
    )
    after_arrival = stored_inputs(dispositions.root)
    with pytest.raises(ValueError, match="pending or UNKNOWN"):
        invoke_tool(comms, "comms_goal", report, caller=ThreadCaller("b"))
    assert stored_inputs(dispositions.root) == after_arrival
    fresh = invoke_tool(
        comms, "comms_inbox", {"thread": "b", "standby": {"goal_id": goal.id, "wait_for": ["a"]}}, caller=NoThreadCaller()
    )["standby_review"]
    assert fresh["reviewed_inputs"] == [f"bus:{messages[1].seq}", f"bus:{late.seq}"]
    invoke_tool(comms, "comms_goal", {**report, "reviewed_inputs": fresh["reviewed_inputs"]}, caller=ThreadCaller("b"))
    assert isinstance(comms.goals.goal_wait("b"), RecordedGoalWait)
    assert stored_inputs(dispositions.root).rows[f"bus:{messages[1].seq}"].declared_name == "reserved"
    assert not stored_inputs(dispositions.root).rows.get("acp:owner-0").goal_reviews


def test_inbox_byte_budget_and_no_ack_keep_the_next_page_unread(inbox_comms):
    comms = inbox_comms
    expected = [comms.messaging.send("a", "b", str(index)).message for index in range(5)]
    page = comms.bus.inbox_at("b", comms.registry.snapshot(), bounds=PageBounds(limit=4, max_bytes=280))
    assert page.page.messages == tuple(expected[:2])
    assert page.page.bytes_read <= 280
    assert page.remaining == 3
    for _ in range(2):
        result = invoke_tool(
            comms, "comms_inbox", {"thread": "b", "limit": 2, "ack": False}, caller=NoThreadCaller(),
        )
        assert result["messages"] == [message.to_wire() for message in expected[:2]]
        assert result["acknowledged"] == 0 and result["remaining"] == 5
        assert comms.bus.pending_count("b") == 5


def test_one_oversized_message_makes_progress_without_a_snapshot(inbox_comms):
    comms = inbox_comms
    first = comms.messaging.send("a", "b", "🧪" * 40_000).message
    second = comms.messaging.send("a", "b", "next page").message
    result = invoke_tool(comms, "comms_inbox", {"thread": "b"}, caller=NoThreadCaller())
    assert result["messages"] == [first.to_wire()]
    assert result["acknowledged"] == result["remaining"] == 1
    result = invoke_tool(comms, "comms_inbox", {"thread": "b"}, caller=NoThreadCaller())
    assert result["messages"] == [second.to_wire()]
    assert result["acknowledged"] == 1 and result["remaining"] == 0
    assert not (comms.root / "tool-output").exists()


@pytest.mark.asyncio
async def test_watch_drains_multiple_pages_without_another_file_event(inbox_comms):
    comms = inbox_comms
    expected = [comms.messaging.send("a", "b", str(index)).message for index in range(201)]
    watcher = comms.bus.watch("b", acknowledgment=ExplicitWatchAcknowledgment)
    try:
        async with asyncio.timeout(5):
            received = [await anext(watcher) for _ in expected]
    finally:
        await watcher.aclose()
    assert received == expected
    assert comms.bus.pending_count("b") == len(expected)
