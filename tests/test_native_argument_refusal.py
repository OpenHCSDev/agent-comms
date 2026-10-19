"""Real native tool errors complete their call; they do not fail the input."""

import asyncio
import json

import pytest

from agent_comms.channel_coding_tools import ArgumentRefusal, CodingCall
from agent_comms.comms import Comms
from agent_comms.native_tool_call import SelectedToolDenied
from agent_comms.pi_events import ToolExecutionEnd, ToolExecutionStart
from agent_comms.channel_coding_tools import CodingToolSocket
from agent_comms.pi_payloads import ToolCallContent
from delivery_owner_fixture import canonical_agent, reply_turn_finished


@pytest.mark.parametrize("name, arguments, encoded", [
    ("bash", {"command": "pwd", "timeout": None}, {"command": "pwd"}),
    ("read", {"path": "x", "offset": None, "limit": None}, {"path": "x"}),
])
def test_pi_optional_nulls_are_omitted_by_the_existing_codec(name, arguments, encoded):
    call = CodingCall.decode("optional-null", name, arguments)
    assert call.allowed_claim() is None
    assert call.tool.arguments == encoded


@pytest.mark.parametrize("request_first", [False, True])
async def test_refused_arguments_complete_without_an_owner_callback(tmp_path, request_first):
    class Owner:
        async def allow(self, round_number, call):
            raise AssertionError("invalid arguments must never acquire authority")

    socket = CodingToolSocket(tmp_path, "a" * 64, Owner())
    args = {"command": False}
    raw = (json.dumps({"token": socket.token, "call_id": "bad", "name": "bash",
                       "arguments": {"command": "false"}}) + "\n").encode()
    pending = asyncio.create_task(socket.handle_request(raw)) if request_first else None
    if request_first:
        await asyncio.sleep(0)
    socket.announce((ToolCallContent(id="bad", name="bash", arguments=args),))
    socket.tool_started(ToolExecutionStart(tool_call_id="bad", tool_name="bash", args=args))
    with pytest.raises(SelectedToolDenied) as denied:
        await (pending if pending is not None else socket.handle_request(raw))
    assert "received bool" in socket.failure_response(denied.value)["error"]
    assert isinstance(socket.calls.require(0, "bad").claim_scope, ArgumentRefusal)
    socket.tool_finished(ToolExecutionEnd(tool_call_id="bad", tool_name="bash", is_error=True), "b" * 32)
    socket.assert_complete()
    assert not (tmp_path / "selected-tool-ledger").exists()


async def test_actual_native_optional_null_then_refusal_then_next_call(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    provider = native.provider
    arguments = (
        {"command": "printf P488_NULL_RAN", "timeout": None},
        "pwd",
        {"command": False},
        {"command": "printf P488_NEXT_RAN"},
    )

    def responses():
        number = provider.posts
        assert 1 <= number <= 5, "no provider retry or original-input replay"
        if number <= len(arguments):
            yield {"tool_calls": [{"index": 0, "id": f"p488-{number}", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps(arguments[number - 1])}}]}, None
            yield {}, "tool_calls"
        else:
            yield {"content": "P488_DONE"}, None
            yield {}, "stop"

    monkeypatch.setattr(provider, "response_chunks", responses)
    owner = canonical_agent(Comms(native.root), auto_wake=True, runtime_enabled=True,
        agent_bin="pi", agent_args=native.native_arguments(()))
    owner.on_connect(None)
    try:
        await owner._runtime.start()
        session = await native.attach_saved_owner(owner, project=native.project, session=native.session)
        comms = owner._comms
        comms.threads.claim_thread("verifier", tags=frozenset(), worktree=str(native.project))
        comms.messaging.send("verifier", session, "Fresh P488 native argument verification.").message
        await owner.inputs.drain_inbox(session)
        await asyncio.wait_for(reply_turn_finished(owner, session), 45)
        assert native.session.read_bytes().startswith(original)
        rows = [json.loads(line) for line in native.session.read_text().splitlines()]
        messages = [row["message"] for row in rows if row.get("type") == "message"]
        tools = [message for message in messages if message.get("role") == "toolResult"]
        assert [tool["toolCallId"] for tool in tools] == ["p488-1", "p488-2", "p488-3", "p488-4"]
        assert not tools[0].get("isError") and "P488_NULL_RAN" in tools[0]["content"][0]["text"]
        assert tools[1]["isError"]
        assert tools[2]["isError"] and "received bool" in tools[2]["content"][0]["text"]
        assert not tools[3].get("isError") and "P488_NEXT_RAN" in tools[3]["content"][0]["text"]
        assert messages[-1]["content"][0]["text"] == "P488_DONE"
        assert len([message for message in messages if message.get("inputId")]) == 1
        assert provider.posts == 5
        print("P488_native_result=" + json.dumps({"root": str(native.root),
            "output": [tool["content"][0]["text"] for tool in tools],
            "answer": "P488_DONE", "provider_posts": provider.posts,
            "new_inputs": 1, "original_history_preserved": True}))
    finally:
        await owner.shutdown()
        assert not owner.turns.turn_tasks and not owner.turns.persistent_backends
