"""One tool declaration controls presentation without granting coding authority."""

import asyncio
import json

import acp
import pytest
from acp.schema import ToolCallStart

from agent_comms import agent_events as events
from agent_comms import backend
from agent_comms.comms import Comms
from agent_comms.tool_declaration import ToolDeclaration, ToolInput
from agent_comms.pi_payloads import ToolCallContent
from delivery_owner_fixture import canonical_agent

pytest_plugins = ("test_backend_native_lifecycle",)


def test_boundaries_keep_missing_file_arguments_strict_and_extension_inputs_opaque():
    from agent_comms.cli_bridges.claude_protocol import ClaudeSession, TimedRecord, ToolUse
    from agent_comms.cli_bridges.opencode_parts import ToolPart

    record = TimedRecord(uuid='record', timestamp='2026-10-16T20:00:00Z')
    call = ToolUse(id='claude', name='Read', input={'file_path': 'claude.py'})
    update, = call.updates(record, ClaudeSession(turn='turn'))
    assert update.accumulator.path == 'claude.py'
    assert update.accumulator.title == 'Read claude.py'
    for name, pattern, title in [('Grep', 'needle', 'Search needle'), ('Glob', '*.py', 'Find *.py')]:
        search, = ToolUse(id=name, name=name, input={'pattern': pattern, 'path': 'src'}).updates(record, ClaudeSession(turn='turn'))
        assert search.accumulator.title == title and search.accumulator.path == 'src'
        saved, _ = search.accumulator.finished('saved', True)
        assert saved.title == title and saved.path == 'src'
    with pytest.raises(ValueError):
        ToolUse(id='wrong', name='Read', input={}).updates(
            record, ClaudeSession(turn='turn'))
    pi, = ToolCallContent('pi', 'read', {'path': 'pi.py'}).assistant_transcript()
    assert pi.path == 'pi.py' and pi.title == 'Read pi.py'
    with pytest.raises((TypeError, ValueError)):
        ToolCallContent('wrong', 'read', {'file_path': 'not-Pi'}).assistant_transcript()
    part = ToolPart.model_validate({'type': 'tool', 'id': 'part', 'sessionID': 's',
        'messageID': 'm', 'callID': 'oc', 'tool': 'read',
        'state': {'status': 'running', 'input': {'filePath': 'oc.py'}}})
    assert part.state.call(part).path == 'oc.py'
    bad = part.model_copy(update={'state': part.state.model_copy(update={'input': {}})})
    with pytest.raises(ValueError):
        bad.state.call(bad)
    # An extension's opaque argument with a familiar name is not our schema.
    opaque = {'file_path': {'not': 'a filename'}, 'command': ['not', 'a shell command']}
    unknown = ToolUse(id='opaque', name='external_custom', input=opaque)
    update, = unknown.updates(record, ClaudeSession(turn='turn'))
    assert update.accumulator.title == 'external_custom'
    assert update.accumulator.arguments.raw_input == opaque and update.accumulator.path is None
    with pytest.raises(ValueError, match='no declared caption argument: command'):
        ToolUse(id='missing', name='Bash', input={}).updates(record, ClaudeSession(turn='turn'))


def test_core_commands_share_native_identity_without_changing_external_decode():
    from agent_comms.channel_coding_tools import CodingCall
    from agent_comms.tools import CommsSendTool, ToolRequest

    for request in ToolRequest.members_with(ToolRequest):
        arguments = {"from": "source", "to": "destination", "body": "decode-only"}
        call = CodingCall.decode("isolated-call", request.declared_name, arguments)
        assert call.tool is request and call.arguments == arguments
        assert call.tool.claim is None
    from agent_comms.field_codec import FieldCodec

    assert isinstance(FieldCodec.decode(ToolRequest, {"tool": "comms_send", "from": "source",
        "to": "destination", "body": "decode-only"}), CommsSendTool)
    with pytest.raises(ValueError):
        ToolRequest.decode("read")


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
    # The declared label/detail own the title; rawInput retains the native argument.
    # The SDK reports raw arguments, not a caption. Presentation does not validate
    # or reinterpret that object as an execution request; disclosure retains it.
    expected_title = ToolDeclaration.caption('read', ToolDeclaration.for_name('read').recorded_arguments(ToolInput, {'path': str(source)}))
    assert 'owned.txt' in expected_title
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
    owner = canonical_agent(Comms(tmp_path / "sdk-wire"), auto_wake=False)
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
