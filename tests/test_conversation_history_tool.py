"""The connector reads one conversation through the existing bounded pager."""
import pytest

from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import invoke_tool


def conversation(comms):
    for name in ("claude-ai", "commander", "user", "other"):
        comms.registry.declare(comms.registry.new_thread(
            name=name, tags=frozenset(), worktree="/work",
        ))
    expected = []
    for number in range(5):
        sender, target = (("claude-ai", "commander") if number % 2 == 0
                          else ("commander", "claude-ai"))
        expected.append(comms.messaging.send(sender, target, f"message {number}").message)
        comms.messaging.send("other", "commander", f"unrelated {number}")
    return expected


def test_newest_first_pages_are_exclusive_and_conversation_scoped(comms):
    expected = conversation(comms)
    caller = ThreadCaller("claude-ai")
    first = invoke_tool(comms, "comms_history", {"peer": "commander", "limit": 2}, caller=caller)
    second = invoke_tool(comms, "comms_history", {
        "peer": "commander", "limit": 2, "before": first["before"],
    }, caller=caller)
    assert [m["id"] for m in first["dm"]] == [m.message_id for m in reversed(expected[-2:])]
    assert [m["id"] for m in second["dm"]] == [m.message_id for m in reversed(expected[-4:-2])]
    assert first["has_older"] and second["has_older"]
    assert len(first["dm"]) == len(second["dm"]) == 2


def test_explicit_user_conversation_does_not_read_the_callers_conversation(comms):
    conversation(comms)
    sent = comms.messaging.send("user", "commander", "from user").message
    page = invoke_tool(comms, "comms_history", {
        "peer": "commander", "participant": "user", "limit": 2,
    }, caller=ThreadCaller("claude-ai"))
    assert [m["id"] for m in page["dm"]] == [sent.message_id]
    assert page["has_older"] is False


def test_history_refuses_a_channel_target(comms):
    conversation(comms)
    with pytest.raises((ValueError, KeyError)):
        invoke_tool(comms, "comms_history", {"peer": "#any"}, caller=ThreadCaller("claude-ai"))
