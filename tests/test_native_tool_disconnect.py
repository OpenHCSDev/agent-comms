"""A real saved Pi child abandons its authenticated tool check on a scratch bus."""

import asyncio
import json
import signal

import pytest

from agent_comms.channel_coding_tools import CodingToolOwner
from agent_comms.field_codec import FieldCodec
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_tool_transport import ToolTransport
from agent_comms.pi_rpc import PiRpcChannel
from delivery_owner_fixture import reply_turn_finished


async def test_actual_pi_child_aborts_its_tool_socket(native_backend, monkeypatch, caplog):
    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    (native.project / "readable.txt").write_text("No tool execution is needed for this check.\n")
    handled = asyncio.Event()
    connections, grants, children = [], [], []
    handle = ToolTransport._handle
    allow = CodingToolOwner.allow
    next_event = PiRpcChannel.next_event

    async def observe_connection(socket, reader, writer):
        connections.append((socket, reader))
        try:
            return await handle(socket, reader, writer)
        finally:
            handled.set()

    async def finish_check_after_disconnect(owner, turn, round_number, call):
        reservation = await allow(owner, turn, round_number, call)
        child = turn.native.proc
        assert child.pid == connections[0][0].expected_pid
        children.append(FieldCodec.encode(child.identity))
        # The attested child on this scratch bus dies mid-check; its kernel
        # closes the real authenticated socket. No provider/input is replayed.
        child.platform.send(child.identity, signal.SIGKILL)
        assert await asyncio.wait_for(connections[0][1].read(1), 10) == b""
        grants.append(call.call_id)
        return reservation

    async def eof_after_pending_reply(channel):
        event = await next_event(channel)
        if event is None and connections:
            # Observe child failure after its pending socket callback. Otherwise
            # turn retirement cancels that callback before its response attempt.
            await asyncio.wait_for(handled.wait(), 10)
        return event

    def chunks():
        assert native.provider.posts == 1, "No provider retry or tool replay"
        yield {"tool_calls": [{"index": 0, "id": "disconnect-check", "type": "function",
            "function": {"name": "read", "arguments": json.dumps({"path": "readable.txt"})}}]}, None
        yield {}, "tool_calls"

    monkeypatch.setattr(native.provider, "response_chunks", chunks)
    monkeypatch.setattr(ToolTransport, "_handle", observe_connection)
    monkeypatch.setattr(CodingToolOwner, "allow", finish_check_after_disconnect)
    monkeypatch.setattr(PiRpcChannel, "next_event", eof_after_pending_reply)
    async with native.open_owner(runtime_enabled=True, auto_wake=True, native_options=()) as (owner, session):
        comms = owner._comms
        comms.threads.claim_thread("verifier", tags=frozenset(), worktree=str(native.project))
        comms.messaging.send("verifier", session, "Fresh isolated owner socket disconnect verification.").message
        await owner.inputs.drain_inbox(session)
        with pytest.raises(NativePiUnavailable, match="native exit=-9"):
            await asyncio.wait_for(reply_turn_finished(owner, session), 40)

        assert len(connections) == 1 and grants == ["disconnect-check"] and handled.is_set()
        assert native.provider.posts == 1
        assert native.session.read_bytes().startswith(original)
        messages = [row["message"] for line in native.session.read_text().splitlines()
                    if (row := json.loads(line)).get("type") == "message"]
        tools = [message for message in messages if message.get("role") == "toolResult"]
        assert not tools
        assert len([message for message in messages if message.get("inputId")]) == 1
        assert not connections[0][0]._handlers

    warnings = [record for record in caplog.records
                if record.name == "agent_comms.native_tool_transport" and record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "answer (allowed) could be sent" in warnings[0].getMessage()
    assert "Unhandled exception" not in caplog.text and "BrokenPipeError" not in caplog.text
    print("P228_OWNER_WARNING " + warnings[0].getMessage())
    print("P228_LIVE_RESULT " + json.dumps({
        "root": str(native.root), "child_identities": children,
        "expected_native_failure": "native exit=-9", "input_replayed": False,
        "owner_checks": grants, "warnings": len(warnings), "provider_requests": native.provider.posts,
        "original_history_preserved": True, "new_inputs": 1, "tool_replays": 0,
        "children_retired": all(child.retired for child in native.children),
    }))
