"""Provider-free Pi extension UI relay controls over a real detached child pipe."""

import asyncio
import json
import sys

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.pi_payloads import McpLiveAcknowledgement
from agent_comms.owner_protocol import OwnerLine, OwnerPermissionRequest
from agent_comms.runtime import SocketClient

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable stub"),
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


