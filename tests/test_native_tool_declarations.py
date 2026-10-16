"""One tool declaration controls presentation without granting coding authority."""

import asyncio
import json

import acp
import pytest
from acp.schema import ToolCallStart

from agent_comms import agent_events as events
from agent_comms import backend
from agent_comms.comms import Comms
from agent_comms.native_tools import BashTool, CodingTool, EditTool, ReadTool, ReplacementEdit, WriteTool
from agent_comms.tool_declaration import ToolArguments, ToolCall, ToolDeclaration, UndeclaredTool
from agent_comms.recorded_time import UnrecordedEventTime
from agent_comms.pi_payloads import ToolCallContent
from delivery_owner_fixture import canonical_agent

pytest_plugins = ("test_backend_native_lifecycle",)


async def test_new_tool_declaration_drives_presentation_without_coding_permission(acp_tools):
    class InspectFixtureTool(ToolDeclaration):
        acp_kind = "search"
        label = "Inspect"

    event = events.ToolStart.from_call(ToolArguments.build_call(
        UnrecordedEventTime(), "case", "inspect_fixture", {"query": "new declaration"},
        detail="new declaration"))
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
        CodingTool.from_call("inspect_fixture", {"query": "new declaration"}, call_id="case")
    examples = (ReadTool(path="file"), BashTool(command="pwd"),
        EditTool(path="file", edits=(ReplacementEdit("before", "after"),)),
        WriteTool(path="file", content="value"))
    assert {type(tool) for tool in examples} == set(CodingTool.members_with(CodingTool))
    for tool in examples:
        call = type(tool).from_call(tool.declared_name, tool.arguments, call_id="case")
        event = events.ToolStart.from_call(ToolArguments.build_call(
            UnrecordedEventTime(), "case", tool.declared_name, call.tool.arguments))
        assert event.kind == tool.acp_kind and event.args == tool.arguments
        assert call.tool == tool and tool.detail in event.title
    unknown = events.ToolStart.from_call(ToolArguments.build_call(
        UnrecordedEventTime(), "unknown", "external_extension", {"private": "preserved"}))
    assert (unknown.title, unknown.kind, unknown.args) == (
        "external_extension",
        "other",
        {"private": "preserved"},
    )


@pytest.mark.parametrize("name,label,kind,caption", [
    ("read", "Read", "read", "Read external.txt"), ("bash", "Run", "execute", "Run"),
    ("edit", "Edit", "edit", "Edit external.txt"), ("write", "Write", "edit", "Write external.txt"),
])
def test_recorded_metadata_does_not_require_an_executable_request(name, label, kind, caption):
    from agent_comms.channel_coding_tools import CodingCall
    from agent_comms.native_tool_call import SelectedToolDenied
    from agent_comms.transcript_events import ToolEndTranscript, ToolStartTranscript

    foreign = {"file_path": "external.txt"}
    declared_path = "external.txt" if kind in {"read", "edit"} else None
    start = ToolStartTranscript(tool_call_id="saved", tool_name=name, raw_input=foreign,
                                path=declared_path)
    end = ToolEndTranscript(tool_call_id="saved", tool_name=name, text="saved result")
    assert (start.title, start.kind, start.raw_input) == (caption, kind, foreign)
    assert (end.title, end.kind) == (label, kind)
    assert ToolCall(UnrecordedEventTime(), "saved", name, foreign, path=declared_path).title == caption
    assert ToolStartTranscript(tool_call_id="scalar", tool_name=name, raw_input=17).title == label
    with pytest.raises(SelectedToolDenied):
        CodingCall.decode("execution", name, foreign)


def test_input_caption_is_bounded_and_never_parses_an_execution_request(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Presentation must not decode an executable coding request')

    monkeypatch.setattr(CodingTool, 'parse_arguments', forbidden)
    arguments = {'path': 'foreign.txt', 'payload': 'x' * 1_000_000}
    caption = ToolArguments.build_call(UnrecordedEventTime(), 'opaque', 'read', arguments).title
    assert 'foreign.txt' in caption and len(caption) < 200
    assert arguments['payload'] == 'x' * 1_000_000
    assert len(ToolArguments.build_call(UnrecordedEventTime(), 'command', 'bash', {'command': 'x' * 1_000_000}).title) <= ToolDeclaration.CAPTION_CHARS
    plain = ToolArguments.build_call(UnrecordedEventTime(), 'plain', 'bash', {'command': 'echo "hello"\nnext line'}).title
    assert plain == 'Run echo "hello" next line'


def test_foreign_protocol_owners_supply_their_own_recorded_detail():
    from agent_comms.cli_bridges.codex_protocol import FileChange, UpdatedFile
    from agent_comms.cli_bridges.opencode_parts import ToolPart
    from agent_comms.cli_bridges.shared import ToolCall
    from agent_comms.field_codec import FieldCodec
    from agent_comms.transcript_events import TranscriptEvent

    start, end = FileChange(path="codex.txt", diff="@@ -1 +1 @@\\n-old\\n+new\\n",
                            kind=UpdatedFile()).events("codex", True)
    assert start.title == "Edit codex.txt" and start.raw_input == {"path": "codex.txt"}
    assert start.path == "codex.txt"
    assert end.title == "Edit" and end.diff is not None
    assert FieldCodec.decode(TranscriptEvent, FieldCodec.encode(start)) == start
    part = ToolPart.model_validate({"type": "tool", "id": "part", "sessionID": "session",
        "messageID": "message", "callID": "open-code", "tool": "read",
        "state": {"status": "pending", "input": {"filePath": "open-code.txt"}}})
    call = part.state.call(part)
    assert call.title == "Read open-code.txt"
    recorded, _ = call.finished("saved", True)
    assert recorded.title == call.title and recorded.raw_input == {"filePath": "open-code.txt"}
    assert recorded.path == "open-code.txt"
    scalar, _ = ToolCall(UnrecordedEventTime(), "opaque", "external_custom", ["not", "Pi"]).finished("saved", True)
    assert scalar.title == "external_custom" and scalar.raw_input == ["not", "Pi"]


def test_each_boundary_decodes_its_file_input_without_guessing_another_format():
    from agent_comms.cli_bridges.claude_protocol import ClaudeSession, TimedRecord, ToolUse
    from agent_comms.cli_bridges.opencode_parts import ToolPart

    record = TimedRecord(uuid='record', timestamp='2026-10-16T20:00:00Z')
    call = ToolUse(id='claude', name='Read', input={'file_path': 'claude.py'})
    update, = call.updates(record, ClaudeSession(turn='turn'))
    assert update.accumulator.path == 'claude.py'
    assert update.accumulator.title == 'Read claude.py'
    with pytest.raises(ValueError):
        ToolUse(id='wrong', name='Read', input={'path': 'not-Claude'}).updates(
            record, ClaudeSession(turn='turn'))
    pi, = ToolCallContent('pi', 'read', {'path': 'pi.py'}).assistant_transcript()
    assert pi.path == 'pi.py' and pi.title == 'Read pi.py'
    with pytest.raises((TypeError, ValueError)):
        ToolCallContent('wrong', 'read', {'file_path': 'not-Pi'}).assistant_transcript()
    part = ToolPart.model_validate({'type': 'tool', 'id': 'part', 'sessionID': 's',
        'messageID': 'm', 'callID': 'oc', 'tool': 'read',
        'state': {'status': 'pending', 'input': {'filePath': 'oc.py'}}})
    assert part.state.call(part).path == 'oc.py'
    bad = part.model_copy(update={'state': part.state.model_copy(update={'input': {'path': 'not-OpenCode'}})})
    with pytest.raises(ValueError):
        bad.state.call(bad)
    # An extension's opaque argument with a familiar name is not our schema.
    opaque = {'file_path': {'not': 'a filename'}, 'command': ['not', 'a shell command']}
    unknown = ToolUse(id='opaque', name='external_custom', input=opaque)
    update, = unknown.updates(record, ClaudeSession(turn='turn'))
    assert update.accumulator.title == 'external_custom'
    assert update.accumulator.arguments == opaque and update.accumulator.path is None


def test_core_commands_share_native_identity_without_changing_external_decode():
    from agent_comms.channel_coding_tools import CodingCall
    from agent_comms.native_tools import NativeExecutableTool
    from agent_comms.tools import CommsSendTool, ToolRequest

    assert ToolRequest.__registry__ is CodingTool.__registry__ is ToolDeclaration.__registry__
    assert isinstance(ToolDeclaration.for_name("EXTERNAL_UNKNOWN"), UndeclaredTool)
    assert ToolDeclaration.for_name("EXTERNAL_UNKNOWN").label == "EXTERNAL_UNKNOWN"
    assert "undeclared" not in ToolDeclaration.names()
    assert all(isinstance(member.label, str) for member in ToolDeclaration.members_with(ToolDeclaration))
    assert set(NativeExecutableTool.members_with(NativeExecutableTool)) == (
        set(CodingTool.members_with(CodingTool)) | set(ToolRequest.members_with(ToolRequest))
    )
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
    expected_title = ToolDeclaration.caption('read', path=str(source))
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
