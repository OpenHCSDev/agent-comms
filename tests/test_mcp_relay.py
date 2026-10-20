"""Provider-free Pi extension UI relay controls over a real detached child pipe."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

import agent_comms.agent_events as ae
import agent_comms.pi_events as pi
from agent_comms.field_codec import FieldCodec
from agent_comms.native_pi import CAPABILITY
from agent_comms.pi_payloads import McpLiveAcknowledgement
from agent_comms.owner_protocol import OwnerLine, OwnerPermissionRequest
from agent_comms.runtime import SocketClient
from pi_session_turn import one_turn_events

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable stub"),
]


def stub(tmp_path: Path, body: str) -> str:
    path = tmp_path / "pi-rpc-stub"
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize(
    "method,choice,answer",
    [
        ("confirm", pi.ConfirmedUiChoice(True), {"confirmed": True}),
        ("confirm", None, {"cancelled": True}),
        ("confirm", pi.ValueUiChoice("forged"), {"cancelled": True}),
        ("select", pi.ValueUiChoice("one"), {"value": "one"}),
        ("select", pi.ValueUiChoice("forged"), {"cancelled": True}),
    ],
)
@pytest.mark.usefixtures("native_rpc_fixture")
async def test_same_child_ui_reply_is_correlated_and_denied_without_controller(
    tmp_path, method, choice, answer
):
    marker = tmp_path / "received.json"
    program = stub(
        tmp_path,
        f"""
import json, sys
send = lambda row: print(json.dumps(row), flush=True)
state = json.loads(sys.stdin.readline())
send({{"id": state["id"], "type": "response", "command": "get_state", "success": True,
      "data": {{"nativeInputProofCapability": {json.dumps(CAPABILITY)},
                "sessionId": "fixture-pi-session", "sessionFile": "/fixture/one.jsonl"}}}})
prompt = json.loads(sys.stdin.readline())
send({{"id": prompt["id"], "type": "response", "command": "prompt", "success": True}})
send({{"type": "message_start", "message": {{"role": "user", "content": prompt["message"],
      "inputId": prompt["inputId"]}}}})
send({
            json.dumps(
                {
                    "type": "extension_ui_request",
                    "id": "owned-ui-1",
                    "method": method,
                    "title": "Run one operation?",
                    "message": "exact request",
                    "options": ["one", "two"],
                }
            )
        })
reply = json.loads(sys.stdin.readline())
with open({str(marker)!r}, "w") as output: json.dump(reply, output)
send({{"type": "message_end", "message": {{"role": "assistant", "stopReason": "stop"}}}})
send({{"type": "agent_settled"}})
""",
    )
    requests = []

    async def controller(request):
        requests.append(request)
        return choice

    events = await asyncio.wait_for(
        collect_backend(program, tmp_path, controller if choice is not None else None), timeout=6
    )
    assert marker.exists()
    assert json.loads(marker.read_text()) == {
        "type": "extension_ui_response",
        "id": "owned-ui-1",
        **answer,
    }
    assert len(requests) == (0 if choice is None else 1)
    assert isinstance(events[-1], ae.Done)


async def collect_backend(program, cwd, controller):
    return [
        event
        async for event in one_turn_events(
            program, [], "fixture", str(cwd), ui_request=controller
        )
    ]


def test_live_receipt_rejects_stale_input_malformed_and_ambiguous_claims():
    input_id = "a" * 32
    valid = {
        "version": 1,
        "source": "pi-mcp-client",
        "inputId": input_id,
        "state": "running",
        "lifetime": "turn",
        "servers": [
            {
                "id": "one",
                "scope": "user",
                "state": "ready",
                "calls": "confirm",
                "tools": 1,
                "resources": 0,
                "prompts": 0,
            }
        ],
    }

    assert McpLiveAcknowledgement.from_status(json.dumps(valid), input_id) == FieldCodec.decode(
        McpLiveAcknowledgement,
        valid,
    )
    assert McpLiveAcknowledgement.from_status(json.dumps(valid), "b" * 32) is None
    assert (
        McpLiveAcknowledgement.from_status(
            json.dumps(valid).replace('"version": 1', '"version": 1, "version": 1'),
            input_id,
        )
        is None
    )
    valid["servers"][0]["calls"] = "automatic"
    valid["servers"][0]["state"] = "denied"
    assert McpLiveAcknowledgement.from_status(json.dumps(valid), input_id) is None
    valid["servers"][0]["calls"] = "unavailable"
    valid["servers"][0]["id"] = "\x1b[2J"
    assert McpLiveAcknowledgement.from_status(json.dumps(valid), input_id) is None


async def test_explicit_owner_cancellation_is_not_swallowed_by_socket_permission():
    attached = asyncio.get_running_loop().create_future()
    server = await asyncio.start_server(lambda reader, writer: attached.set_result(writer), "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
    server_writer = await attached
    subscriber = SocketClient(server_writer)
    try:
        pending = asyncio.create_task(subscriber.permission({"toolCallId": "call"}, []))
        for _ in range(10):
            if subscriber.pending:
                break
            await asyncio.sleep(0)
        assert subscriber.pending
        pending.cancel()  # Cancellation can precede the socket handoff's yield.
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not subscriber.pending
        assert isinstance(OwnerLine.read(await reader.readline()), OwnerPermissionRequest)
    finally:
        writer.close(); server_writer.close()
        await writer.wait_closed(); await server_writer.wait_closed()
        server.close(); await server.wait_closed()


