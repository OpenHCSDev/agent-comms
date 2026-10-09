import asyncio
import json
import os
from pathlib import Path

import pytest

from agent_comms import agent_events as ae
from agent_comms import invoke_tool
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import TextRouteUpdate, decode_updates
from delivery_owner_fixture import canonical_agent
from agent_comms.comms import wire
from agent_comms.routing import MessageRoute
from agent_comms.threads import Thread

pytest_plugins = ("test_backend_native_lifecycle",)


def test_incoming_route_distinguishes_channel_and_direct_delivery():
    assert MessageRoute("user", ("#experiment",)).incoming_scope == "#experiment"
    assert MessageRoute("user", ("worker",)).incoming_scope == "direct message"


@pytest.mark.parametrize("target", ["#test", "peer"])
async def test_sent_tool_message_is_visible_live_and_in_saved_history(
    tmp_path, monkeypatch, target
):
    comms = wire(tmp_path / "wire")
    agent = canonical_agent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    await agent.new_session(str(tmp_path / "worker"))
    comms.threads.register(Thread("peer", frozenset({"test"}), str(tmp_path)))
    session = tmp_path / "session.jsonl"
    session.touch()
    comms.threads.attach_session("worker", str(session))
    updates = []
    receipts = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    agent.sessions.client = Client()

    async def events(*args, **kwargs):
        yield ae.ToolStart(id="send1", name="comms_send")
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
        yield ae.ToolEnd(id="send1", name="comms_send", ok=True, output=json.dumps(receipt))
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
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
            event
            for event in comms.transcripts.thread_transcript_page("worker").events
            if event.declared_name == "sent"
        ]
        assert len(replay) == 1 and replay[0].text == "Actual sent text"
        assert replay[0].routing.reply.targets == (target,)
        legacy = json.dumps({"id": receipts[0]["id"]})
        assert (
            comms.messaging.sent_tool_message("comms_send", legacy, True).body == "Actual sent text"
        )
        assert comms.messaging.sent_tool_message("comms_send", legacy, False) is None
        assert comms.messaging.sent_tool_message("another_tool", legacy, True) is None
    finally:
        await agent.shutdown()


async def test_route_is_forwarded_live_and_preserved_by_entry_id(native_backend, monkeypatch):
    native = native_backend
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    comms = wire(native.root)
    agent = CommsAgent(
        comms, agent_bin="pi", runtime_enabled=True, auto_wake=False,
        private_nk_native_package=Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]),
        private_nk_wire_root_id=os.environ["AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"],
        agent_args=["--provider", "response-local", "--model", "fixture", "--thinking", "off",
                    "--offline", "--no-extensions", "--no-skills", "--no-context-files",
                    "--no-prompt-templates", "--no-tools"],
    )
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    agent.on_connect(Client())
    try:
        await agent.new_session(str(native.project))
        name = native.project.name
        comms.channels.update_tags(name, add=frozenset({"test"}))
        comms.threads.attach_session(name, str(native.session))
        message = comms.messaging.send_user_message(
            "#test", "Question in channel", worktree=str(native.project)
        )
        await agent.turns.run_agent_turn(
            name, name, message.body, reply_targets=(message.target,), origins=(message,),
        )
        routes = [fact.route for update in updates for fact in decode_updates(update.field_meta)
                  if isinstance(fact, TextRouteUpdate) and fact.route is not None]
        assert routes and all(route.targets == ("#test",) for route in routes)
        page = wire(native.root).transcripts.thread_transcript_page(name)
        assert page.events[0].text == "Question in channel"
        assert page.events[0].routing.requests[0].sender == "user"
        assert page.events[-1].routing.reply.outgoing_label == "To #test"
        # A real private turn with identical assistant text must not inherit a route.
        await agent.turns.run_agent_turn(name, name, "Private request")
        page = wire(native.root).transcripts.thread_transcript_page(name)
        assert page.events[-1].text == "Native response lifecycle."
        assert page.events[-1].routing is None
        assert native.provider.posts == 2
    finally:
        await agent.shutdown()


async def test_coordination_context_does_not_override_scheduled_response_policy(tmp_path):
    comms = wire(tmp_path / "wire")
    agent = canonical_agent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    await agent.new_session(str(tmp_path / "worker"))
    seen: dict = {}

    async def events(_bin, _args, task, _worktree, _env, **kwargs):
        seen["task"] = task
        seen.update(kwargs)
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="")

    import agent_comms.backend as backend_module

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(backend_module, "stream_agent_events", events)
    try:
        await agent.turns.run_agent_turn("worker", "worker", "incoming broadcast")
    finally:
        monkeypatch.undo()
        await agent.shutdown()
    assert "Response policy:" not in seen["task"]
    assert "mentioned_only" not in seen["task"]
