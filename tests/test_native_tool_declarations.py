"""One tool declaration controls presentation without granting coding authority."""

from dataclasses import dataclass
from pathlib import Path
import asyncio
import json

import acp
import pytest
from acp.schema import ToolCallStart

import agent_comms.agent_events as events
import agent_comms.backend as backend
from agent_comms.comms import Comms
from agent_comms.native_tools import BashTool, CodingTool, EditTool, ReadTool, WriteTool
from agent_comms.tool_declaration import ReplacementEdit
from agent_comms.tool_declaration import ToolDeclaration, UndeclaredTool
from delivery_owner_fixture import canonical_agent



async def test_new_tool_declaration_drives_presentation_without_coding_permission(acp_tools):
    @dataclass(frozen=True, kw_only=True)
    class InspectFixtureTool(ToolDeclaration):
        acp_kind = "search"
        label = "Inspect"
        query: str

        def reads(self):
            return self.query

    event = events.ToolStart.from_call("case", "inspect_fixture", {"query": "new declaration"})
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
        event = events.ToolStart.from_call("case", tool.declared_name, call.tool.arguments)
        assert event.kind == tool.acp_kind and event.args == tool.arguments
        assert call.tool == tool and tool.reads() in event.title
    unknown = events.ToolStart.from_call("unknown", "external_extension", {"private": "preserved"})
    assert (unknown.title, unknown.kind, unknown.args) == (
        "external_extension",
        "other",
        {"private": "preserved"},
    )


@pytest.mark.parametrize("name,label", [("grep", "Search"), ("glob", "Find")])
async def test_pattern_search_metadata_reaches_official_acp(acp_tools, name, label):
    from agent_comms.transcript_events import ToolStartTranscript

    arguments = {"pattern": "shared preview"}
    event = events.ToolStart.from_call("pattern-case", name, arguments)
    recorded = ToolStartTranscript(tool_call_id="pattern-case", tool_name=name, raw_input=arguments,
                                   tool=ToolDeclaration.record(name, arguments))
    assert (event.title, event.kind) == (f"{label} shared preview", "search")
    assert (recorded.title, recorded.kind) == (event.title, event.kind)
    owner, received = acp_tools
    await owner._emit_event("pattern-tool", event, owner.sessions.client)
    async with asyncio.timeout(3):
        update = await received.get()
    assert isinstance(update, ToolCallStart)
    assert (update.kind, update.title, update.raw_input) == (
        "search", f"{label} shared preview", arguments,
    )
    with pytest.raises(ValueError):
        CodingTool.from_call(name, arguments, call_id="pattern-case")


@pytest.mark.parametrize("name,label,kind", [
    ("read", "Read", "read"), ("bash", "Run", "execute"),
    ("edit", "Edit", "edit"), ("write", "Write", "edit"),
])
def test_recorded_metadata_does_not_require_an_executable_request(name, label, kind):
    from agent_comms.channel_coding_tools import CodingCall
    from agent_comms.native_tool_call import SelectedToolDenied
    from agent_comms.transcript_events import ToolEndTranscript, ToolStartTranscript

    foreign = {"file_path": "external.txt"}
    start = ToolStartTranscript(tool_call_id="saved", tool_name=name, raw_input=foreign)
    end = ToolEndTranscript(tool_call_id="saved", tool_name=name, text="saved result")
    assert (start.title, start.kind, start.raw_input) == (label, kind, foreign)
    assert (end.title, end.kind) == (label, kind)
    assert events.ToolStart.from_call("saved", name, foreign).title == label
    from agent_comms.field_codec import FieldCodec
    for invalid in (17, None, [["path", "external.txt"]]):
        with pytest.raises((TypeError, ValueError)):
            FieldCodec.decode(ToolStartTranscript, {**FieldCodec.encode(start), "raw_input": invalid})
    with pytest.raises(SelectedToolDenied):
        CodingCall.decode("execution", name, foreign)


def test_foreign_protocol_owners_decode_canonical_preview_arguments():
    from agent_comms.cli_bridges.codex_protocol import FileChange, UpdatedFile
    from agent_comms.cli_bridges.opencode_parts import ToolPart
    from agent_comms.cli_bridges.shared import ToolCall
    from agent_comms.field_codec import FieldCodec
    from agent_comms.native_tools import FilePatchTool
    from agent_comms.transcript_events import TranscriptEvent

    start, end = FileChange(path="codex.txt", diff="@@ -1 +1 @@\\n-old\\n+new\\n",
                            kind=UpdatedFile()).events("codex", True)
    assert start.title == "Edit codex.txt" and start.raw_input == {"path": "codex.txt"}
    assert start.tool == FilePatchTool(path="codex.txt")
    assert end.title == "Edit" and end.diff is not None
    assert FieldCodec.decode(TranscriptEvent, FieldCodec.encode(start)) == start
    part = ToolPart.model_validate({"type": "tool", "id": "part", "sessionID": "session",
        "messageID": "message", "callID": "open-code", "tool": "read",
        "state": {"status": "pending", "input": {"filePath": "open-code.txt"}}})
    call = part.state.call(part)
    assert call.title == "Read open-code.txt" and call.tool == ReadTool(path="open-code.txt")
    recorded, _ = call.finished("saved", True)
    assert recorded.title == call.title and recorded.raw_input == {"path": "open-code.txt"}
    opaque = {"external": ["not", "Pi"]}
    scalar, _ = ToolCall("opaque", "external_custom", opaque).finished("saved", True)
    assert scalar.title == "external_custom" and scalar.raw_input == opaque


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
                root=Path(str(native.project)), session_file=str(native.session),
                persistent_session=native.persistent,
                native_start=native.started,
            )
        ]
    assert records[-1].ok, records[-1]
    starts = [event for event in records if isinstance(event, events.ToolStart)]
    ends = [event for event in records if isinstance(event, events.ToolEnd)]
    assert len(starts) == len(ends) == 1
    # Declaration-owned argument fields supply the title without execution parsing.
    # The SDK reports raw arguments, not a caption. Presentation does not validate
    # or reinterpret that object as an execution request; disclosure retains it.
    expected_title = f"Read {source}"
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
