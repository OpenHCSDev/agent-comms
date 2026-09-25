import asyncio
import json

import pytest

from agent_comms import MessageRoute, Thread, invoke_tool, wire
from agent_comms.acp import CommsAgent


def test_incoming_route_distinguishes_channel_and_direct_delivery():
    assert MessageRoute("user", ("#experiment",)).incoming_scope == "#experiment"
    assert MessageRoute("user", ("worker",)).incoming_scope == "direct message"


@pytest.mark.parametrize("target", ["#test", "peer"])
async def test_sent_tool_message_is_visible_live_and_in_saved_history(
    tmp_path, monkeypatch, target
):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    await agent.new_session(str(tmp_path / "worker"))
    comms.register(Thread("peer", frozenset({"test"}), str(tmp_path)))
    session = tmp_path / "session.jsonl"
    session.touch()
    comms.attach_session("worker", str(session))
    updates = []
    receipts = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    agent._client = Client()

    async def events(*args, **kwargs):
        yield {"type": "tool_start", "id": "send1", "name": "comms_send"}
        receipt = invoke_tool(
            comms,
            "comms_send",
            {
                "from": "worker",
                "to": target,
                "body": "Actual sent text",
            },
        )
        receipts.append(receipt)
        session.write_text(
            json.dumps(
                {
                    "type": "message",
                    "id": "tool-result",
                    "message": {
                        "role": "toolResult",
                        "toolName": "comms_send",
                        "toolCallId": "send1",
                        "content": [{"type": "text", "text": json.dumps(receipt)}],
                    },
                }
            )
            + "\n"
        )
        yield {
            "type": "tool_end",
            "id": "send1",
            "name": "comms_send",
            "ok": True,
            "output": json.dumps(receipt),
        }
        yield {"type": "settled"}
        yield {"type": "done", "ok": True}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent.prompt("worker", [{"type": "text", "text": "!agent send this"}])
        sent = [
            update
            for update in updates
            if (update.field_meta or {}).get("agentComms", {}).get("route")
        ]
        assert len(sent) == 1 and sent[0].content.text == "Actual sent text"
        assert sent[0].field_meta["agentComms"]["route"]["targets"] == (target,)
        replay = [
            event for event in comms.thread_transcript_page("worker").events if event.kind == "sent"
        ]
        assert len(replay) == 1 and replay[0].text == "Actual sent text"
        assert replay[0].routing.reply.targets == (target,)
        legacy = json.dumps({"id": receipts[0]["id"]})
        assert comms.sent_tool_message("comms_send", legacy, True).body == "Actual sent text"
        assert comms.sent_tool_message("comms_send", legacy, False) is None
        assert comms.sent_tool_message("another_tool", legacy, True) is None
    finally:
        await agent.shutdown()


async def test_route_is_forwarded_live_and_preserved_by_entry_id(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", agent_args=[], runtime_enabled=True)
    await agent.new_session(str(tmp_path / "worker"))
    comms.update_tags("worker", add=frozenset({"test"}))
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session","id":"session","version":3}\n')
    comms.attach_session("worker", str(session))
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    agent._client = Client()

    async def events(*args, **kwargs):
        native_id = "a" * 32
        with kwargs["send_boundary"](None, native_id, args[2]) as allowed:
            assert allowed
        assert kwargs["native_start"](None, native_id, args[2])
        with session.open("a") as output:
            for identity, role, content in (
                ("u1", "user", args[2]),
                ("a1", "assistant", "Channel answer"),
            ):
                output.write(
                    json.dumps(
                        {
                            "type": "message",
                            "id": identity,
                            "message": {
                                "role": role,
                                "content": content,
                                **({"inputId": native_id} if role == "user" else {}),
                            },
                        }
                    )
                    + "\n"
                )
        yield {"type": "chunk", "text": "Channel answer"}
        yield {"type": "settled"}
        yield {"type": "done", "ok": True}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        comms.send_user_message("#test", "Question in channel", worktree=str(tmp_path))
        await agent._drain_inbox("worker")
        await asyncio.wait_for(agent._wake_tasks["worker"], 2)
        routed = [
            update
            for update in updates
            if (update.field_meta or {}).get("agentComms", {}).get("route")
        ]
        assert routed[0].field_meta["agentComms"]["route"]["targets"] == ("#test",)
        page = wire(tmp_path / "wire").thread_transcript_page("worker")
        assert page.events[0].text == "Question in channel"
        assert page.events[0].routing.requests[0].sender == "user"
        assert page.events[-1].routing.reply.outgoing_label == "To #test"
        # Equal private text must not inherit routing from matching content.
        with session.open("a") as output:
            output.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": "private",
                        "message": {"role": "assistant", "content": "Channel answer"},
                    }
                )
                + "\n"
            )
        assert wire(tmp_path / "wire").thread_transcript_page("worker").events[-1].routing is None
    finally:
        await agent.shutdown()


async def test_coordination_context_does_not_override_scheduled_response_policy(tmp_path):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    await agent.new_session(str(tmp_path / "worker"))
    seen: dict = {}

    async def events(_bin, _args, task, _worktree, _env, **kwargs):
        seen["task"] = task
        seen.update(kwargs)
        yield {"type": "settled"}
        yield {"type": "done", "ok": True}

    import agent_comms.acp as acp_module

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(acp_module.backend, "stream_agent_events", events)
    try:
        await agent._run_agent_turn("worker", "worker", "incoming broadcast")
    finally:
        monkeypatch.undo()
        await agent.shutdown()
    assert "Response policy:" not in seen["task"]
    assert "mentioned_only" not in seen["task"]
