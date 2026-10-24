"""One tool declaration controls presentation without granting coding authority."""


import asyncio
import json
from dataclasses import dataclass

import acp
import pytest
from acp.schema import ToolCallStart

import agent_comms.agent_events as events
from agent_comms.acp import SdkSessionConnection
from agent_comms.agent_event_updates import AcpEventConsumer
from agent_comms.comms import wire
from agent_comms.native_tools import BashTool, CodingTool, EditTool, ReadTool, WriteTool
from agent_comms.tool_declaration import (
    EditAction,
    ExecuteAction,
    OtherAction,
    ReadAction,
    ReplacementEdit,
    SearchAction,
    ToolDeclaration,
    UndeclaredTool,
)
from conftest import transcript_records
from delivery_owner_fixture import canonical_agent


async def test_new_tool_declaration_drives_presentation_without_coding_permission(acp_tools):
    @dataclass(frozen=True, kw_only=True)
    class InspectFixtureTool(SearchAction, ToolDeclaration):
        label = "Inspect"
        query: str

        def reads(self):
            return self.query

    event = events.ToolStart.from_call("case", "inspect_fixture", {"query": "new declaration"})
    assert (event.title, event.kind) == ("Inspect new declaration", SearchAction)
    owner, received = acp_tools
    await AcpEventConsumer(owner, "declared-tool", owner.sessions.client).dispatch(event)
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
    examples = (ReadTool(call_id="case", path="file"), BashTool(call_id="case", command="pwd"),
        EditTool(call_id="case", path="file", edits=(ReplacementEdit("before", "after"),)),
        WriteTool(call_id="case", path="file", content="value"))
    assert {type(tool) for tool in examples} == set(CodingTool.members_with(CodingTool))
    for tool in examples:
        call = type(tool).from_call(tool.declared_name, tool.arguments, call_id="case")
        assert type(call) is type(tool)
        event = events.ToolStart.from_call("case", tool.declared_name, call.arguments)
        assert event.kind is tool.declared_action() and event.args == tool.arguments
        assert call.call_id == "case" and call.arguments == tool.arguments and tool.reads() in event.title
    unknown = events.ToolStart.from_call("unknown", "external_extension", {"private": "preserved"})
    assert (unknown.title, unknown.kind, unknown.args) == (
        "external_extension",
        OtherAction,
        {"private": "preserved"},
    )


@pytest.mark.parametrize("name,label", [("grep", "Search"), ("glob", "Find")])
async def test_pattern_search_metadata_reaches_official_acp(acp_tools, name, label):
    from agent_comms.transcript_events import ToolStartTranscript

    arguments = {"pattern": "shared preview"}
    event = events.ToolStart.from_call("pattern-case", name, arguments)
    recorded = transcript_records.event(ToolStartTranscript, position=0, tool_call_id="pattern-case", tool_name=name, raw_input=arguments, tool=ToolDeclaration.record(name, arguments, call_id="pattern-case"))
    assert (event.title, event.kind) == (f"{label} shared preview", SearchAction)
    assert (recorded.title, recorded.kind) == (event.title, event.kind)
    owner, received = acp_tools
    await AcpEventConsumer(owner, "pattern-tool", owner.sessions.client).dispatch(event)
    async with asyncio.timeout(3):
        update = await received.get()
    assert isinstance(update, ToolCallStart)
    assert (update.kind, update.title, update.raw_input) == (
        "search", f"{label} shared preview", arguments,
    )
    with pytest.raises(ValueError):
        CodingTool.from_call(name, arguments, call_id="pattern-case")


@pytest.mark.parametrize("name,label,kind", [
    ("read", "Read", ReadAction), ("bash", "Run", ExecuteAction),
    ("edit", "Edit", EditAction), ("write", "Write", EditAction),
])
def test_recorded_metadata_does_not_require_an_executable_request(name, label, kind):
    from agent_comms.native_tool_call import CodingCall
    from agent_comms.tool_denial import SelectedToolDenied
    from agent_comms.transcript_events import ToolEndTranscript, ToolStartTranscript

    foreign = {"file_path": "external.txt"}
    start = transcript_records.event(ToolStartTranscript, position=0, tool_call_id="saved", tool_name=name, raw_input=foreign)
    end = transcript_records.event(ToolEndTranscript, position=1, tool_call_id="saved", tool_name=name, text="saved result")
    assert (start.title, start.kind, start.raw_input) == (label, kind, foreign)
    assert (end.title, end.kind) == (label, kind)
    assert events.ToolStart.from_call("saved", name, foreign).title == label
    from agent_comms.field_codec import FieldCodec
    for invalid in (17, None, [["path", "external.txt"]]):
        with pytest.raises((TypeError, ValueError)):
            FieldCodec.decode(ToolStartTranscript, {**FieldCodec.encode(start), "raw_input": invalid})
    refused = CodingCall.decode("execution", name, foreign)
    with pytest.raises(SelectedToolDenied):
        refused.allowed_claim()


def test_foreign_protocol_owners_decode_canonical_preview_arguments():
    from agent_comms.cli_bridges.codex_protocol import FileChange, UpdatedFile
    from agent_comms.cli_bridges.opencode_parts import ToolPart
    from agent_comms.field_codec import FieldCodec
    from agent_comms.native_tools import FilePatchTool
    from agent_comms.tool_calls import ToolCall
    from agent_comms.transcript_events import TranscriptEvent

    start, end = FileChange(path="codex.txt", diff="@@ -1 +1 @@\\n-old\\n+new\\n",
                            kind=UpdatedFile()).events("codex", True, transcript_records.timestamp.at.timestamp())
    assert start.title == "Edit codex.txt" and start.raw_input == {"path": "codex.txt"}
    assert start.tool == FilePatchTool(path="codex.txt")
    assert end.title == "Edit" and end.diff is not None
    assert FieldCodec.decode(TranscriptEvent, FieldCodec.encode(start)) == start
    part = ToolPart.model_validate({"type": "tool", "id": "part", "sessionID": "session",
        "messageID": "message", "callID": "open-code", "tool": "read",
        "state": {"status": "pending", "input": {"filePath": "open-code.txt"}}})
    call = part.state.call(part)
    assert call.title == "Read open-code.txt" and call.tool == ReadTool(call_id="open-code", path="open-code.txt")
    recorded, _ = call.finished("saved", True, 0, transcript_records.timestamp.at.timestamp())
    assert recorded.title == call.title and recorded.raw_input == {"path": "open-code.txt"}
    opaque = {"external": ["not", "Pi"]}
    scalar, _ = ToolCall("opaque", "external_custom", opaque).finished("saved", True, 0, transcript_records.timestamp.at.timestamp())
    assert scalar.title == "external_custom" and scalar.raw_input == opaque


def test_core_commands_share_native_identity_without_changing_external_decode():
    from agent_comms.native_tool_call import CodingCall
    from agent_comms.native_tools import (
        NativeExecutableTool,
        NativeRequestTools,
        NativeToolDeclaration,
    )
    from agent_comms.tool_request import ToolRequest
    from agent_comms.tools import CommsSendTool

    schemas = NativeRequestTools.catalog()
    assert {schema["name"] for schema in schemas} == {
        request.declared_name for request in ToolRequest.members_with(ToolRequest)
    }
    assert json.loads(json.dumps(schemas)) == schemas
    assert ToolRequest.__registry__ is CodingTool.__registry__ is ToolDeclaration.__registry__
    assert isinstance(ToolDeclaration.for_name("EXTERNAL_UNKNOWN"), UndeclaredTool)
    assert ToolDeclaration.for_name("EXTERNAL_UNKNOWN").label == "EXTERNAL_UNKNOWN"
    assert "undeclared" not in ToolDeclaration.names()
    assert all(isinstance(member.label, str) for member in ToolDeclaration.members_with(ToolDeclaration))
    assert set(NativeToolDeclaration.members_with(NativeToolDeclaration)) == (
        set(CodingTool.members_with(CodingTool)) | set(ToolRequest.members_with(ToolRequest))
    )
    for request in ToolRequest.members_with(ToolRequest):
        arguments = {"to": "destination", "body": "decode-only"}
        call = CodingCall.decode("isolated-call", request.declared_name, arguments)
        assert isinstance(call, NativeExecutableTool) and call.declared_name == request.declared_name
        assert call.call_id == "isolated-call" and call.arguments == arguments
        assert call.allowed_claim() is None
        assert call.digest() == f'{request.declared_name} {json.dumps(arguments)}'
    from agent_comms.field_codec import FieldCodec

    assert isinstance(FieldCodec.decode(ToolRequest, {"tool": "comms_send",
        "to": "destination", "body": "decode-only"}), CommsSendTool)
    with pytest.raises(ValueError):
        ToolRequest.member_named("read")


def test_native_call_fields_are_not_model_arguments():
    from agent_comms.field_codec import FieldCodec
    from agent_comms.native_tool_call import CodingCall
    from agent_comms.tool_denial import SelectedToolDenied

    arguments = {"path": "file.py", "content": "value"}
    call = CodingCall.decode("owned-call", "write", arguments)
    assert type(call) is WriteTool and call.call_id == "owned-call"
    assert call.arguments == arguments
    assert FieldCodec.encode(call) == {"tool": "write", **arguments}
    assert set(FieldCodec.record_schema(WriteTool)["properties"]) == set(arguments)
    refused = CodingCall.decode("owned-call", "write", {**arguments, "call_id": "foreign"})
    with pytest.raises(SelectedToolDenied):
        refused.allowed_claim()


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
            connection = SdkSessionConnection(owner, writer, reader, listening=False)
            try:
                await connection.listen()
            finally:
                await asyncio.shield(connection.close())
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
