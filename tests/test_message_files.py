"""A message's files: uploaded with the send, listed and read through the message, never on
its envelope, and carried by every installation."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.message_files import (
    MAX_MESSAGE_FILE_BYTES,
    FileUpload,
    MessageFileContent,
    MessageFiles,
    MessageFilesRead,
)
from agent_comms.messaging.message_page import MessagePage
from agent_comms.messaging.published_message import MessagePublished
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import DurableFile, SQLiteFile
from agent_comms.storage.typed_table import ReconciledFile
from agent_comms.transport.connector import Connector
from agent_comms.transport.connector_store import ConnectorStore, UiAccess
from agent_comms.transport.mcp_wire import ToolsCallMessage, ToolsCallReply
from agent_comms.transport.request import ToolCall, ToolRequest
from agent_comms.transport.tool_caller import HumanCaller, ThreadCaller
from agent_comms.transport.tools.send import CommsSendTool
from agent_comms.transport.served_tools.served import (
    ServedCommsConversationPageTool,
    ServedCommsMessageFilesTool,
    ServedCommsMessageFileTool,
    ServedCommsUserSendTool,
)
from agent_comms.transport.tools.sidebar import (
    CommsMessageFilesTool,
    CommsMessageFileTool,
    CommsUserSendTool,
)
from agent_comms_hooks import ToolRefusedError
from bus_record_fixture import prepare_native_comms


def upload(name: str, content: bytes, mime_type: str = "application/octet-stream") -> FileUpload:
    return FileUpload(name, mime_type, base64.b64encode(content).decode("ascii"))


@pytest.fixture
def threads(comms: Comms) -> Comms:
    prepare_native_comms(comms)
    for name, tags in (("recipient", {"general"}), ("outsider", set())):
        comms.registry.declare(NativeThread(name, frozenset(tags), Checkout(comms.root)))
    return comms


def user(comms: Comms) -> str:
    return comms.messaging.user_identity(Checkout(comms.root)).name


def send_human(comms: Comms, target: str, body: str, *files: FileUpload) -> MessagePublished:
    return HumanCaller().invoke(ServedCommsUserSendTool(target=target, body=body, files=files), comms)


async def post[R](client: TestClient, token: str, request: ToolRequest[R], number: int) -> R:
    call = ToolsCallMessage(jsonrpc="2.0", id=number, params=ToolCall(
        request.declared_name, FieldCodec.encode_fields(request)))
    response = await client.post("/mcp", json=FieldCodec.encode(call),
                                 headers={"Authorization": f"Bearer {token}"})
    assert response.status == 200
    reply = FieldCodec.decode(ToolsCallReply, await response.json()).result
    assert reply.is_error is False, reply.content
    return FieldCodec.decode(request.result_type(), reply.structured_content)


async def test_a_file_sent_from_a_remote_ui_is_the_file_its_recipient_reads(
    threads: Comms, tmp_path: Path,
) -> None:
    """Over the connector, as the phone sends: a file over the transport's default 1 MiB body
    rides the human send, the UI lists it beside its page, and the agent it went to reads the
    same bytes back through the message."""
    comms = threads
    content = os.urandom(2 * 1024 * 1024 + 3)
    sent = upload("trace.bin", content)
    store = ConnectorStore.at(tmp_path / "auth")
    namespace = comms.registry.store.privacy().namespace
    token = store.update(lambda grants: grants.ui_issued(namespace, 60, UiAccess)).token
    async with TestServer(Connector(store, "http://127.0.0.1:18766", comms.root).application()) as server:
        async with TestClient(server) as client:
            published = await post(client, token, CommsUserSendTool(
                target="recipient", body="the trace", files=(sent,)), 1)
            me = user(comms)
            listed = await post(client, token, CommsMessageFilesTool(
                conversation="recipient", participant=me, messages=(published.message.source,)), 2)
            (file,) = listed.files
            assert (file.message, file.position, file.name, file.size) == (
                published.message.source, 0, "trace.bin", len(content))
            read = await post(client, token, CommsMessageFileTool(
                conversation="recipient", participant=me, message=file.message, position=0), 3)
    assert base64.b64decode(read.data) == content
    assert read.file == file
    fetched = ThreadCaller("recipient").invoke(ServedCommsMessageFileTool(
        conversation=me, message=published.message.source, position=0), comms)
    assert isinstance(fetched, MessageFileContent)
    assert base64.b64decode(fetched.data) == content
    (stored,) = comms.bus.inbox("recipient").page.messages
    assert stored.body == "the trace"


def test_an_agent_send_carries_files_after_its_sender_retires(threads: Comms) -> None:
    comms = threads
    sent = CommsSendTool(target="recipient", body="the log", files=(upload("log.txt", b"line\n", "text/plain"),))
    published = ThreadCaller("outsider").invoke(sent, comms)
    assert "files" not in FieldCodec.encode(CommsSendTool(target="recipient", body="none"))
    request = ServedCommsMessageFilesTool(
        conversation="outsider", messages=(published.message.source,)
    )
    listed = ThreadCaller("recipient").invoke(request, comms)
    comms.registry.remove("outsider")
    retained = ThreadCaller("recipient").invoke(request, comms)
    assert isinstance(listed, MessageFilesRead)
    assert retained == listed
    assert [(file.name, file.mime_type, file.size) for file in listed.files] == [
        ("log.txt", "text/plain", 5)
    ]


def test_files_over_the_message_limit_are_refused_before_anything_is_written(threads: Comms) -> None:
    comms = threads
    with pytest.raises(ValueError):
        upload("big.bin", b"x" * (MAX_MESSAGE_FILE_BYTES + 1))
    with pytest.raises(ValueError):
        FieldCodec.decode(CommsUserSendTool, {
            "to": "recipient", "body": "big",
            "files": [{"name": "big.bin", "mimeType": "application/octet-stream",
                       "data": base64.b64encode(b"x" * (MAX_MESSAGE_FILE_BYTES + 1)).decode()}],
        })
    before = comms.bus.bus.revision()
    half = MAX_MESSAGE_FILE_BYTES // 2 + 1
    with pytest.raises(ValueError):
        send_human(comms, "recipient", "two halves", upload("a.bin", b"a" * half), upload("b.bin", b"b" * half))
    assert comms.bus.bus.revision() == before
    assert not MessageFiles(comms.root).path.exists()
    for name, mime_type, data in (("../escape", "text/plain", "aGk="), ("..", "text/plain", "aGk="),
                                  (".", "text/plain", "aGk="), ("a\\b", "text/plain", "aGk="),
                                  ("note.txt", "not a type", "aGk="),
                                  ("note.txt", "text/plain", "not base64!"), ("empty.txt", "text/plain", "")):
        with pytest.raises(ValueError):
            FileUpload(name, mime_type, data)


def test_a_reader_outside_the_conversation_is_refused_its_files(threads: Comms) -> None:
    """Files are read by the rule that reads their message: a DM's files only by its
    participants, a channel's by whoever reads the channel."""
    comms = threads
    published = send_human(comms, "recipient", "for you", upload("secret.txt", b"secret", "text/plain"))
    me = user(comms)
    for request in (
        ServedCommsMessageFileTool(conversation=me, message=published.message.source, position=0),
        ServedCommsMessageFilesTool(conversation=me, messages=(published.message.source,)),
        ServedCommsMessageFileTool(conversation="#general", message=published.message.source, position=0),
    ):
        with pytest.raises(ToolRefusedError):
            ThreadCaller("outsider").invoke(request, comms)
    with pytest.raises(ToolRefusedError):
        ThreadCaller("recipient").invoke(ServedCommsMessageFileTool(
            conversation=me, message=published.message.source, position=1), comms)
    posted = ThreadCaller("recipient").invoke(CommsSendTool(
        target="#general", body="for all", files=(upload("notes.txt", b"notes", "text/plain"),)), comms)
    read = ThreadCaller("outsider").invoke(ServedCommsMessageFileTool(
        conversation="#general", message=posted.message.source, position=0), comms)
    assert base64.b64decode(read.data) == b"notes"


def test_a_page_of_messages_with_files_is_the_page_a_reader_before_files_decodes(threads: Comms) -> None:
    """Files are rows beside the bus, so the page and every message on it encode exactly as
    they did before files existed: no key a decoder that rejects unknown fields could refuse.
    tools/old_reader_decodes_files.py runs the installed runtime's own decoder over the same
    page."""
    comms = threads
    send_human(comms, "recipient", "with a file", upload("a.png", b"\x89PNG", "image/png"))
    send_human(comms, "recipient", "without one")
    page = HumanCaller().invoke(ServedCommsConversationPageTool(
        conversation=CommandTarget.named("recipient"), project=Checkout(comms.root)), comms)
    encoded = json.dumps(FieldCodec.encode(page))
    assert '"files"' not in encoded and "mimeType" not in encoded
    decoded = FieldCodec.decode(MessagePage, json.loads(encoded))
    assert [message.body for message in decoded.messages] == ["with a file", "without one"]


def test_message_files_are_a_carried_bus_file_the_installer_reconciles(threads: Comms) -> None:
    comms = threads
    published = send_human(comms, "recipient", "kept", upload("kept.txt", b"kept", "text/plain"))
    path = MessageFiles(comms.root).path
    assert path in DurableFile.paths_in(comms.root)
    assert ReconciledFile.existing_in(comms.root)[path] is MessageFiles
    with closing(MessageFiles.writer(path, isolation_level=None)) as db:
        with SQLiteFile.write_transaction(db):
            MessageFiles.carry_schema(db)
        assert not MessageFiles.schema_differs(db)
    (file,) = MessageFiles(comms.root).of((published.message.source,))
    assert base64.b64decode(MessageFiles(comms.root).content(file).data) == b"kept"
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM file_content").fetchone() == (1,)
