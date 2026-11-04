"""A session on another machine is woken by a pushed frame, through the login it already has."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from agent_comms.activity import ActivityState, ObservedActivity
from agent_comms.cli_bridges.inbox_watch_client import FollowedTranscript, InboxSocket
from agent_comms.comms import Comms
from agent_comms.channel_targets import CommandTarget
from agent_comms.connector import Connector
from agent_comms.connector_store import ConnectorStore
from agent_comms.repo import Checkout
from agent_comms.thread_status import ConnectedThreadStatus, NoBridgeThreadStatus
from agent_comms.threads import ExternalThread, NativeThread
from agent_comms.tool_caller import ThreadCaller
from agent_comms.cli_bridges.shared import MonitorReadFile
from agent_comms.tools import CommsInboxTool


@pytest.fixture
async def session(tmp_path: Path, comms: Comms):
    """A live connector over a real store, holding the Director login a claude.ai session has."""
    comms.registry.declare(ExternalThread("claude-ai", frozenset(), Checkout(comms.root)))
    comms.registry.declare(NativeThread("sender", frozenset(), Checkout(comms.root)))
    store = ConnectorStore.at(tmp_path / "auth")
    connector = Connector(store, "http://127.0.0.1:18767", comms.root)
    async with TestServer(connector.application()) as server:
        async with TestClient(server) as client:
            with store.granted("test") as token:
                yield client, token


async def watch_path(client: TestClient, token: str, name: str) -> str:
    """The URL the tool hands the model, as the path this test server's client connects to."""
    response = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "comms_watch", "arguments": {"fork": {"name": name}}}})
    reply = await response.json()
    assert not reply["result"]["isError"], reply
    url = URL(reply["result"]["structuredContent"]["url"])
    assert url.scheme == "ws"
    return url.path


async def frame(socket) -> dict:
    return json.loads(await asyncio.wait_for(socket.receive_str(), timeout=30))


async def test_the_cloud_client_delivers_a_message_as_a_notice_naming_its_whole_body(
    session, comms: Comms, tmp_path: Path, capsys
) -> None:
    client, token = session
    url = str(client.make_url(await watch_path(client, token, "cloud")))
    exports = tmp_path / "state" / "messages"
    exports.parent.mkdir(mode=0o700)
    watch = asyncio.create_task(InboxSocket(url, exports).carried())
    comms.messaging.send("sender", "cloud", "a body longer than one line\nsecond line")
    async with asyncio.timeout(30):
        while not (exports.is_dir() and any(exports.iterdir())):
            assert not watch.done(), watch.result()
            await asyncio.sleep(0.05)
    watch.cancel()
    lines = capsys.readouterr().out.splitlines()
    notice = next(json.loads(line) for line in lines if line.startswith("{"))
    assert notice["type"] == "read-file"
    written = Path(notice["path"]).read_text()
    assert "Comms in · sender" in written and "second line" in written


async def test_a_burst_of_pushed_messages_never_shares_one_monitor_batch(
    session, comms: Comms, tmp_path: Path
) -> None:
    """Monitor cut one joined batch of 11 notices, so the last of them never reached the session."""
    client, token = session
    url = str(client.make_url(await watch_path(client, token, "cloud")))
    exports = tmp_path / "state" / "messages"
    exports.parent.mkdir(mode=0o700)
    for number in range(4):
        comms.messaging.send("sender", "cloud", f"burst {number}")
    watch = asyncio.create_task(InboxSocket(url, exports).carried())
    async with asyncio.timeout(30):
        while not exports.is_dir() or len(list(exports.iterdir())) < 4:
            assert not watch.done(), watch.result()
            await asyncio.sleep(0.05)
    watch.cancel()
    written = sorted(path.stat().st_mtime for path in exports.iterdir())
    assert all(later - earlier >= 0.2 for earlier, later in zip(written, written[1:]))


async def turns_of(client: TestClient, token: str, thread: str) -> str:
    response = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "comms_turns", "arguments": {"thread": thread, "fork": {"name": thread}}}})
    return json.dumps(await response.json())


def transcript_lines(words: str) -> tuple[str, ...]:
    recorded = "2026-11-04T00:00:00.000Z"
    return (
        json.dumps({"type": "user", "uuid": str(uuid4()), "timestamp": recorded, "sessionId": "s",
                    "promptId": "p", "origin": {"kind": "human"}, "message": {"content": "start"}}),
        json.dumps({"type": "assistant", "uuid": str(uuid4()), "timestamp": recorded,
                    "message": {"content": [{"type": "text", "text": words}]}}),
    )


async def test_a_transcript_sent_up_the_socket_shows_as_that_threads_work(session, comms: Comms) -> None:
    """Toad showed a remote session as connected with nothing in its chat."""
    client, token = session
    socket = await client.ws_connect(await watch_path(client, token, "cloud"))
    for line in transcript_lines("work the session did remotely"):
        await socket.send_str(line)
    async with asyncio.timeout(20):
        while "work the session did remotely" not in await turns_of(client, token, "cloud"):
            await asyncio.sleep(0.1)
    await socket.close()


def running_tool_line() -> str:
    return json.dumps({"type": "assistant", "uuid": str(uuid4()), "timestamp": "2026-11-04T00:00:01.000Z",
                       "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                "input": {"command": "sleep 60"}}]}})


async def test_activity_comes_from_the_same_lines_as_the_chat_once_the_window_has_been_sent(
    session, comms: Comms, tmp_path: Path
) -> None:
    """Toad's chat updated while its activity stayed idle: nothing ended the reader's replay."""
    client, token = session
    url = str(client.make_url(await watch_path(client, token, "cloud")))
    source = tmp_path / "session.jsonl"
    source.write_text("".join(f"{line}\n" for line in transcript_lines("before")))
    watch = asyncio.create_task(
        InboxSocket(url, tmp_path / "messages", FollowedTranscript(source)).carried())

    def activity() -> ObservedActivity:
        return comms.agents.activity_of("cloud", snapshot=comms.registry.snapshot())

    async with asyncio.timeout(30):
        while "before" not in await turns_of(client, token, "cloud"):
            assert not watch.done(), watch.result()
            await asyncio.sleep(0.05)
        with source.open("a") as stream:
            stream.write(f"{running_tool_line()}\n")
        while activity().state is not ActivityState.WORKING:
            assert not watch.done(), watch.result()
            await asyncio.sleep(0.05)
    assert activity().detail == "Run sleep 60"

    def stop_hooks_summary(*, output: bool) -> str:
        return json.dumps({"type": "system", "subtype": "stop_hook_summary", "uuid": str(uuid4()),
                           "timestamp": "2026-11-04T00:00:02.000Z", "hasOutput": output})

    async with asyncio.timeout(30):
        with source.open("a") as stream:
            stream.write(f"{stop_hooks_summary(output=True)}\n")
            stream.write(f"{transcript_lines('the hook spoke, so the turn goes on')[1]}\n")
        while "the hook spoke" not in await turns_of(client, token, "cloud"):
            await asyncio.sleep(0.05)
        assert activity().state is not ActivityState.IDLE
        with source.open("a") as stream:
            stream.write(f"{stop_hooks_summary(output=False)}\n")
        while activity().state is not ActivityState.IDLE:
            assert not watch.done(), watch.result()
            await asyncio.sleep(0.05)
    watch.cancel()


async def test_a_transcript_record_core_cannot_read_is_said_and_keeps_the_push(
    session, comms: Comms
) -> None:
    client, token = session
    comms.channels.create_tag("comms")
    socket = await client.ws_connect(await watch_path(client, token, "cloud"))
    await socket.send_str(json.dumps({"type": "assistant", "not": "a record Core knows"}))
    await asyncio.sleep(0.5)
    comms.messaging.send("sender", "cloud", "still pushed")
    assert (await frame(socket))["text"] == "still pushed"
    await socket.close()
    said = comms.views.history_page(CommandTarget.named("#comms")).messages[-1].body
    assert "ValidationError" in said and "TaskGroup" not in said


async def test_a_monitor_notice_cut_short_in_the_transcript_does_not_stop_the_transcript(
    session, comms: Comms
) -> None:
    """Every re-arm replays its window, so one cut notice stopped that session's work each time."""
    client, token = session
    socket = await client.ws_connect(await watch_path(client, token, "cloud"))
    message = comms.messaging.send("sender", "cloud", "pushed before the cut notice").message
    cut = MonitorReadFile.naming(Path("/root/messages"), message.reference)
    await socket.send_str(json.dumps({
        "type": "user", "uuid": str(uuid4()), "timestamp": "2026-11-04T00:00:00.000Z", "sessionId": "s",
        "promptId": "p", "origin": {"kind": "task-notification"},
        "message": {"content": [{"type": "text", "text": (
            "<task-notification><task-id>m</task-id><summary>Monitor event: \"watch\"</summary>"
            f"<event>{cut.model_dump_json()[:150]}</event></task-notification>")}]},
    }))
    await socket.send_str(transcript_lines("after the cut notice")[1])
    async with asyncio.timeout(20):
        while "after the cut notice" not in await turns_of(client, token, "cloud"):
            await asyncio.sleep(0.05)
    await socket.close()


def test_the_cloud_client_never_sends_a_watch_url_up_to_the_bus() -> None:
    url = "wss://ts-gaming.tail553874.ts.net/watch/AbC-d_123"
    line = FollowedTranscript.line(json.dumps({"url": url, "command": f"watch {url}"}).encode())
    assert "AbC-d_123" not in line
    assert json.loads(line)["url"] == "<inbox watch URL>"


async def test_the_cloud_client_says_out_loud_when_its_url_is_refused(session, tmp_path: Path) -> None:
    client, _ = session
    used = str(client.make_url("/watch/not-a-ticket"))
    ended = await InboxSocket(used, tmp_path / "messages").carried()
    assert ended.startswith("failed: WSServerHandshakeError")


async def inbox_emptied(comms: Comms, thread: str) -> None:
    """A frame goes out before its message is recorded as taken, so a test waits for the record."""
    request = CommsInboxTool(thread=thread)
    async with asyncio.timeout(10):
        while request.apply(comms, ThreadCaller(thread)).messages:
            await asyncio.sleep(0.05)


async def test_a_pushed_frame_arrives_and_a_rearmed_watch_does_not_replay_it(
    session, comms: Comms,
) -> None:
    """Both are waking: a message already waiting and one sent while the socket is open."""
    client, token = session
    path = await watch_path(client, token, "cloud")
    comms.messaging.send("sender", "cloud", "first")
    socket = await client.ws_connect(path)
    assert (await frame(socket))["text"] == "first"
    comms.messaging.send("sender", "cloud", "second")
    assert (await frame(socket))["text"] == "second"
    await inbox_emptied(comms, "cloud")
    await socket.close()

    comms.messaging.send("sender", "cloud", "third")
    again = await client.ws_connect(await watch_path(client, token, "cloud"))
    assert (await frame(again))["text"] == "third"
    await again.close()


async def test_the_thread_reads_as_bridged_only_while_its_socket_is_open(
    session, comms: Comms,
) -> None:
    client, token = session
    socket = await client.ws_connect(await watch_path(client, token, "cloud"))

    async def status_is(status: type) -> None:
        async with asyncio.timeout(10):
            while not isinstance(comms.registry.snapshot().entry("cloud").status, status):
                await asyncio.sleep(0.05)

    await status_is(ConnectedThreadStatus)
    await socket.close()
    await status_is(NoBridgeThreadStatus)


async def test_a_watch_url_opens_once(session) -> None:
    client, token = session
    path = await watch_path(client, token, "cloud")
    socket = await client.ws_connect(path)
    with pytest.raises(WSServerHandshakeError):
        await client.ws_connect(path)
    await socket.close()


async def test_a_watch_url_is_no_login(session) -> None:
    """The ticket opens its socket and nothing else: the connector does not know it as a bearer."""
    client, token = session
    ticket = URL(await watch_path(client, token, "cloud")).name
    response = await client.post("/mcp", headers={"Authorization": f"Bearer {ticket}"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response.status == 401


async def test_a_newer_socket_replaces_the_older_one(session, comms: Comms) -> None:
    client, token = session
    older = await client.ws_connect(await watch_path(client, token, "cloud"))
    newer = await client.ws_connect(await watch_path(client, token, "cloud"))
    await asyncio.wait_for(older.receive(), timeout=10)
    assert older.closed
    comms.messaging.send("sender", "cloud", "after")
    assert (await frame(newer))["text"] == "after"
    await newer.close()
