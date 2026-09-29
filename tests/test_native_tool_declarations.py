"""One tool declaration controls presentation without granting coding authority."""

import asyncio
import json

import acp
import pytest
from acp.schema import ToolCallStart

from agent_comms import agent_events as events
from agent_comms import backend
from agent_comms.comms import wire
from agent_comms.native_tools import CodingTool, NativeTool
from delivery_owner_fixture import canonical_agent

pytest_plugins = ("test_backend_native_lifecycle",)


async def test_new_tool_declaration_drives_presentation_without_coding_permission(acp_tools):
    class InspectFixtureTool(NativeTool):
        acp_kind = "search"
        action = "Inspect"

        @classmethod
        def detail(cls, arguments):
            return arguments["query"]

    event = NativeTool.start("case", "inspect_fixture", {"query": "new declaration"})
    assert (event.title, event.kind) == ("Inspect new declaration", "search")
    owner, received = acp_tools
    await owner._emit_event("declared-tool", event, owner.sessions.client)
    async with asyncio.timeout(3):
        update = await received.get()
    assert isinstance(update, ToolCallStart)
    assert (update.kind, update.title, update.raw_input) == (
        "search",
        "Inspect new declaration",
        {"query": "new declaration"},
    )
    with pytest.raises(ValueError):
        CodingTool.from_call("inspect_fixture", {"query": "new declaration"})
    for member in CodingTool.members_with(CodingTool):
        call = member.from_call(member.declared_name, {"path": "file", "command": "pwd"})
        event = NativeTool.start("case", member.declared_name, call.arguments)
        assert event.kind == member.acp_kind and event.args == call.arguments
    unknown = NativeTool.start("unknown", "external_extension", {"private": "preserved"})
    assert (unknown.title, unknown.kind, unknown.args) == (
        "External Extension",
        "other",
        {"private": "preserved"},
    )


async def test_actual_native_read_tool_reaches_official_acp_with_declared_presentation(
    native_backend, monkeypatch, acp_tools
):
    native = native_backend
    source = native.project / "owned.txt"
    source.write_text("ACTUAL_READ_CONTENT\n")
    ordinary = native.provider.handle

    async def request_read(reader, writer):
        if native.provider.posts:
            await ordinary(reader, writer)
            return
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            length = next(
                int(line.split(b":", 1)[1])
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            request = json.loads(await reader.readexactly(length))
            assert any(tool["function"]["name"] == "read" for tool in request["tools"])
            native.provider.posts += 1
            chunk = {
                "id": "native-tool",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "read-proof",
                                    "type": "function",
                                    "function": {
                                        "name": "read",
                                        "arguments": json.dumps({"path": str(source)}),
                                    },
                                }
                            ]
                        },
                    }
                ],
            }
            body = b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    monkeypatch.setattr(native.provider, "handle", request_read)
    async with asyncio.timeout(25):
        records = [
            event
            async for event in backend.stream_agent_events(
                "pi",
                [
                    "--provider",
                    "response-local",
                    "--model",
                    "fixture",
                    "--thinking",
                    "off",
                    "--offline",
                    "--no-extensions",
                    "--no-skills",
                    "--no-context-files",
                    "--no-prompt-templates",
                    "--tools",
                    "read",
                ],
                "Read owned.txt once and answer",
                str(native.project),
                session_file=str(native.session),
                persistent_session=native.persistent,
                native_start=native.started,
            )
        ]
    assert records[-1].ok, records[-1]
    starts = [event for event in records if isinstance(event, events.ToolStart)]
    ends = [event for event in records if isinstance(event, events.ToolEnd)]
    assert len(starts) == len(ends) == 1
    # Activity captions have an existing 120-character detail budget. The
    # external rawInput must still retain the entire native argument below.
    detail = str(source)
    expected_title = "Read " + detail[:120] + ("…" if len(detail) > 120 else "")
    assert starts[0].kind == "read" and starts[0].title == expected_title
    assert ends[0].ok and "ACTUAL_READ_CONTENT" in ends[0].output
    assert len(native.starts) == len(native.saved_inputs()) == 1
    assert native.provider.posts == 2 and source.read_text() == "ACTUAL_READ_CONTENT\n"

    owner, received = acp_tools
    await owner._emit_event("native-tool", starts[0], owner.sessions.client)
    async with asyncio.timeout(3):
        update = await received.get()
    assert isinstance(update, ToolCallStart)
    assert (update.kind, update.title, update.raw_input) == (
        "read",
        expected_title,
        {"path": str(source)},
    )


@pytest.fixture
async def acp_tools(tmp_path):
    owner = canonical_agent(wire(tmp_path / "sdk-wire"), auto_wake=False)
    received = asyncio.Queue()

    class Client(acp.Client):
        async def session_update(self, session_id, update, **kwargs):
            received.put_nowait(update)

    connections = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        try:
            await acp.run_agent(owner, writer, reader)
        finally:
            writer.close()
            await writer.wait_closed()
            connections.remove(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    client = acp.connect_to_agent(Client(), writer, reader)
    try:
        async with asyncio.timeout(10):
            await client.initialize(protocol_version=1)
        yield owner, received
    finally:
        writer.close()
        await writer.wait_closed()
        await client.close()
        server.close()
        for task in tuple(connections):
            task.cancel()
        await asyncio.gather(*tuple(connections), return_exceptions=True)
        await server.wait_closed()
        await owner.shutdown()
