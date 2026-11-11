"""A session on another machine is woken by a pushed frame, through the login it already has."""

import asyncio
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest
from aiohttp import WSMsgType, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer, unused_port
from pydantic import ValidationError
from yarl import URL

from agent_comms.cli_bridges.claude_protocol import (
    ClaudeMonitorFrame,
    MonitorTaskNotification,
    TaskEnvelope,
)
from agent_comms.cli_bridges.claude_transcript import (
    WATCH_DEFERRED,
    WATCH_REPLACED,
    PrintedLedger,
    ReconnectsAlone,
    ReconnectUrl,
)
from agent_comms.cli_bridges.harness_delivery_states import PrintLedgerId
from agent_comms.cli_bridges.inbox_watch import OpenWatch
from agent_comms.cli_bridges.inbox_watch_client import (
    RECONNECTS_ALONE,
    WAITS_FOR_MAINTENANCE,
    WATCHING,
    FollowedTranscript,
    InboxSocket,
    Pause,
    Pauses,
    Redials,
    RuntimeReplaced,
    WatchDeferred,
    WatchDropped,
    WatchEnd,
    WatchReplaced,
    WatchStopped,
)
from agent_comms.cli_bridges.shared import MonitorReadFile
from agent_comms.comms import Comms
from agent_comms.transport.connector import Connector
from agent_comms.coordination.tables.harness_deliveries import HarnessDelivery
from agent_comms.execution.launch_runtime import InstalledSource, LaunchRuntime
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.activity import ActivityState, ObservedActivity
from agent_comms.registry.thread_status import (
    ConnectedThreadStatus,
    NoBridgeThreadStatus,
)
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.storage.locked_store import RegistryFile
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.transport.connector_caller import GrantRefusedError
from agent_comms.transport.connector_store import ConnectorStore
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.inbox import CommsInboxTool


class QuickHeartbeatConnector(Connector):
    """Pings every second, so a frozen client is dropped in about one instead of 25."""

    heartbeat_seconds = 1.0


@pytest.fixture
def connector_type() -> type[Connector]:
    return Connector


@pytest.fixture
async def session(tmp_path: Path, comms: Comms, connector_type: type[Connector]):
    """A live connector over a real store, holding the Director login a claude.ai session has."""
    comms.registry.declare(ExternalThread("claude-ai", frozenset(), Checkout(comms.root)))
    comms.registry.declare(NativeThread("sender", frozenset(), Checkout(comms.root)))
    store = ConnectorStore.at(tmp_path / "auth")
    # The connector names its own address in every URL it issues, and a reconnecting client
    # dials that URL whole, so the server listens where the connector says it does.
    port = unused_port()
    connector = connector_type(store, f"http://127.0.0.1:{port}", comms.root)
    async with TestServer(connector.application(), port=port) as server:
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


async def delivered_lines(client, token: str, comms: Comms, exports: Path, capsys, body: str):
    """What the cloud client prints for one message, as Monitor would show it."""
    url = str(client.make_url(await watch_path(client, token, "cloud")))
    watch = asyncio.create_task(InboxSocket(url, exports).carried())
    comms.messaging.send("sender", "cloud", body)
    lines: list[str] = []
    async with asyncio.timeout(30):
        while not any(line.startswith(("Comms in", '{"protocol"')) for line in lines):
            assert not watch.done(), watch.result()
            await asyncio.sleep(0.05)
            lines += capsys.readouterr().out.splitlines()
    watch.cancel()
    return lines


async def test_the_cloud_client_shows_a_message_that_fits_one_event_as_written(
    session, comms: Comms, tmp_path: Path, capsys
) -> None:
    client, token = session
    exports = tmp_path / "state" / "messages"
    exports.parent.mkdir(mode=0o700)
    lines = await delivered_lines(client, token, comms, exports, capsys, "short body\nsecond line")
    start = next(index for index, line in enumerate(lines) if line.startswith("Comms in"))
    # A direct message names the thread it went to, so a reader tells it from a channel post.
    assert lines[start].startswith("Comms in · cloud · sender (external agent) · ")
    # A reply's answers cite the bus position as well as the id, so the header names both.
    assert re.search(r" · \d+-[0-9a-f]{12}$", lines[start])
    assert lines[start + 1:start + 3] == ["short body", "second line"]
    assert not exports.exists() or not any(exports.iterdir())


async def test_the_cloud_client_names_the_whole_body_of_a_message_too_long_for_one_event(
    session, comms: Comms, tmp_path: Path, capsys
) -> None:
    client, token = session
    exports = tmp_path / "state" / "messages"
    exports.parent.mkdir(mode=0o700)
    body = "a line of a long body\n" * 160
    lines = await delivered_lines(client, token, comms, exports, capsys, body)
    notice = next(json.loads(line) for line in lines if line.startswith('{"protocol"'))
    assert notice["type"] == "read-file"
    written = Path(notice["path"]).read_text()
    assert written.startswith("Comms in · cloud · sender (external agent) · ")
    assert written.count("a line of a long body") == 160


async def test_the_cloud_client_shows_a_message_one_batch_holds_whole_across_wrapped_lines(
    session, comms: Comms, tmp_path: Path, capsys
) -> None:
    """Monitor cuts a line at 500 units and a batch at 3000, and drops an empty line, so a
    2500-character body arrives as written: its 1200-character line wrapped under the cap, its
    blank line kept by a zero-width space Monitor does not trim."""
    client, token = session
    exports = tmp_path / "state" / "messages"
    exports.parent.mkdir(mode=0o700)
    long = " ".join(f"w{index:03d}" for index in range(240))
    body = "\n".join(["first", "", long, "x" * (2500 - len(long) - 8)])
    lines = await delivered_lines(client, token, comms, exports, capsys, body)
    start = next(index for index, line in enumerate(lines) if line.startswith("Comms in"))
    shown = lines[start:]
    assert all(ClaudeMonitorFrame.units(line) <= ClaudeMonitorFrame.EVENT_CHARS for line in shown)
    assert len("\n".join(shown)) <= ClaudeMonitorFrame.BATCH_CHARS
    assert shown[1:3] == ["first", "\u200b"]
    assert "".join(shown[3:6]) == long and "".join(shown[6:]) == body.rsplit("\n", 1)[1]
    assert "\u200b" not in "".join(shown[3:])
    assert not exports.exists() or not any(exports.iterdir())


async def test_indented_code_goes_to_the_file_with_its_indent_byte_exact(
    session, comms: Comms, tmp_path: Path, capsys
) -> None:
    """Monitor trims a line's indent, and a guard character would ride into whatever a reader
    pastes, so a body with an indented line is the file, copied byte for byte."""
    client, token = session
    exports = tmp_path / "state" / "messages"
    exports.parent.mkdir(mode=0o700)
    code = "def indented():\n    return 'four'\n\tif tab:\n        pass"
    lines = await delivered_lines(client, token, comms, exports, capsys, code)
    notice = next(json.loads(line) for line in lines if line.startswith(('Comms in', '{"protocol"')))
    assert notice["type"] == "read-file"
    written = Path(notice["path"]).read_text()
    assert written.endswith(code + "\n") and "\u200b" not in written


def monitor_event_record(event: str) -> str:
    """The transcript record Claude writes when a Monitor event reaches the session."""
    return json.dumps({
        "type": "user", "uuid": str(uuid4()), "timestamp": "2026-11-04T00:00:00.000Z", "sessionId": "s",
        "promptId": "p", "origin": {"kind": "task-notification"},
        "message": {"content": [{"type": "text", "text": (
            "<task-notification><task-id>m</task-id><summary>Monitor event: \"watch\"</summary>"
            f"<event>{event}</event></task-notification>")}]},
    })


async def test_a_message_quoting_our_frames_goes_to_the_file_and_keeps_the_transcript(
    session, comms: Comms, tmp_path: Path, capsys
) -> None:
    """Shown as written, a body line quoting one of our frames would be decoded as one."""
    client, token = session
    exports = tmp_path / "state" / "messages"
    exports.parent.mkdir(mode=0o700)
    quoted = ('{"protocol":"agent-comms-monitor","type":"not-a-frame"}\n'
              'Comms in · someone · 0123456789ab\nnot a part')
    with pytest.raises(ValidationError):
        ClaudeMonitorFrame.decode_event(quoted)
    lines = await delivered_lines(client, token, comms, exports, capsys, quoted)
    event = next(line for line in lines if line.startswith(("Comms in", '{"protocol"')))
    assert json.loads(event)["type"] == "read-file"
    assert Path(json.loads(event)["path"]).read_text().endswith(quoted + "\n")
    socket = await client.ws_connect(await watch_path(client, token, "cloud"))
    await socket.send_str(monitor_event_record(event))
    await socket.send_str(transcript_lines("after the quoting message")[1])
    async with asyncio.timeout(20):
        while "after the quoting message" not in await turns_of(client, token, "cloud"):
            await asyncio.sleep(0.05)
    await socket.close()


async def test_a_burst_of_pushed_messages_never_shares_one_monitor_batch(
    session, comms: Comms, tmp_path: Path
) -> None:
    """Monitor cut one joined batch of 11 notices, so the last of them never reached the session."""
    client, token = session
    url = str(client.make_url(await watch_path(client, token, "cloud")))
    exports = tmp_path / "state" / "messages"
    exports.parent.mkdir(mode=0o700)
    for number in range(4):
        # Too long for one event, so each lands in a file whose time records when it was emitted.
        comms.messaging.send("sender", "cloud", f"burst {number}\n" + "x" * 3100)
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


async def test_a_shown_message_that_is_not_xml_does_not_stop_the_transcript(
    session, comms: Comms
) -> None:
    """A message shown as written said "&&" or "<", and every re-arm replayed it."""
    client, token = session
    socket = await client.ws_connect(await watch_path(client, token, "cloud"))
    await socket.send_str(json.dumps({
        "type": "user", "uuid": str(uuid4()), "timestamp": "2026-11-04T00:00:00.000Z", "sessionId": "s",
        "promptId": "p", "origin": {"kind": "task-notification"},
        "message": {"content": [{"type": "text", "text": (
            "<task-notification><task-id>m</task-id><summary>Monitor event: \"watch\"</summary>"
            "<event>Comms in · b · 1-a\nrun a && b if x < y</event></task-notification>")}]},
    }))
    await socket.send_str(transcript_lines("after the shown message")[1])
    async with asyncio.timeout(20):
        while "after the shown message" not in await turns_of(client, token, "cloud"):
            await asyncio.sleep(0.05)
    await socket.close()


async def test_a_monitor_notice_cut_short_in_the_transcript_does_not_stop_the_transcript(
    session, comms: Comms
) -> None:
    """Every re-arm replays its window, so one cut notice stopped that session's work each time."""
    client, token = session
    socket = await client.ws_connect(await watch_path(client, token, "cloud"))
    message = comms.messaging.send("sender", "cloud", "pushed before the cut notice").message
    cut = MonitorReadFile.naming(Path("/root/messages"), message.reference)
    await socket.send_str(monitor_event_record(cut.model_dump_json()[:150]))
    await socket.send_str(transcript_lines("after the cut notice")[1])
    async with asyncio.timeout(20):
        while "after the cut notice" not in await turns_of(client, token, "cloud"):
            await asyncio.sleep(0.05)
    await socket.close()


def test_the_cloud_client_never_sends_a_watch_url_up_to_the_bus() -> None:
    url = "wss://ts-gaming.tail553874.ts.net/watch/AbC-d_123"
    line = FollowedTranscript.line(json.dumps({"url": url, "command": f"watch {url}"}).encode())
    assert "AbC-d_123" not in line
    assert json.loads(line)["url"] == "[inbox watch URL]"


def test_a_hidden_watch_url_leaves_a_task_notification_readable() -> None:
    """The stand-in was written in angle brackets, inside the XML the notification is."""
    url = "wss://ts-gaming.tail553874.ts.net/watch/AbC-d_123"
    notification = f"<task-notification><event>closed {url}</event></task-notification>"
    line = FollowedTranscript.line(json.dumps({"text": notification}).encode())
    assert isinstance(TaskEnvelope.from_text(json.loads(line)["text"]), MonitorTaskNotification)


async def test_the_cloud_client_says_out_loud_when_its_url_is_refused(session, tmp_path: Path) -> None:
    client, _ = session
    used = str(client.make_url("/watch/not-a-ticket").with_scheme("ws"))
    ended = await InboxSocket(used, tmp_path / "messages").carried()
    assert ended.line().startswith("Your inbox watch failed: WSServerHandshakeError")
    assert "not-a-ticket" not in ended.line()


async def test_a_watch_running_a_replaced_runtime_stops_instead_of_dialling(tmp_path: Path, monkeypatch) -> None:
    """Every install closes the socket; a watch whose runtime the install replaced must not
    re-dial into the new connector with the old client. It ends, and the model starts it anew."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    newer = tmp_path / "runtimes/core-newer"
    newer.mkdir(parents=True)
    pointer = LaunchRuntime.current_pointer()
    pointer.parent.mkdir(parents=True)
    pointer.symlink_to(newer, target_is_directory=True)
    watch = InboxSocket("ws://127.0.0.1:9/watch/never-dialled", tmp_path / "messages",
                        source=InstalledSource(tmp_path / "site"))
    ended = await asyncio.wait_for(watch.carried(), 10)
    assert ended == RuntimeReplaced()
    assert ended.line().startswith("Core was reinstalled") and "comms_watch" in ended.line()


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
    # The replaced socket says so; an unexplained 1006 is a drop someone has to go diagnose.
    assert older.close_code == WATCH_REPLACED
    comms.messaging.send("sender", "cloud", "after")
    assert (await frame(newer))["text"] == "after"
    await newer.close()


async def stopped(client: TestClient, token: str, name: str) -> None:
    """comms_stop as a teammate calls it, through the same connector the watch is on."""
    response = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "comms_stop", "arguments": {"name": name}}})
    assert response.status == 200, await response.text()
    reply = await response.json()
    assert not reply["result"]["isError"], reply


async def status_becomes(comms: Comms, thread: str, status: type) -> None:
    async with asyncio.timeout(10):
        while type(comms.registry.snapshot().entry(thread).status) is not status:
            await asyncio.sleep(0.05)


async def test_a_stop_on_the_bus_ends_the_cloud_watch_and_finishes(
    session, comms: Comms, tmp_path: Path,
) -> None:
    """No owner runtime serves a cloud thread, so the stop releases the watch's binding, and the
    watch tells the session not to re-arm; it used to dial an owner runtime and fail with a 500."""
    client, token = session
    url = str(client.make_url(await watch_path(client, token, "cloud")))
    watch = asyncio.create_task(InboxSocket(url, tmp_path / "messages").carried())
    await status_becomes(comms, "cloud", ConnectedThreadStatus)
    await stopped(client, token, "cloud")
    assert await asyncio.wait_for(watch, timeout=30) == WatchStopped()
    assert type(comms.registry.snapshot().entry("cloud").status) is NoBridgeThreadStatus


async def test_a_stop_finishes_with_no_session_on_the_other_end(session, comms: Comms) -> None:
    """A reclaimed VM leaves nothing to answer: the stop still ends stopped, never Stopping."""
    client, token = session
    socket = await client.ws_connect(await watch_path(client, token, "cloud"))
    await status_becomes(comms, "cloud", ConnectedThreadStatus)
    await socket.close()
    await status_becomes(comms, "cloud", NoBridgeThreadStatus)
    await stopped(client, token, "cloud")
    assert type(comms.registry.snapshot().entry("cloud").status) is NoBridgeThreadStatus


def test_an_install_or_a_newer_watch_still_says_rearm() -> None:
    """Only the stop code means stay off; an install's drop and a replacement mean come back."""
    assert WatchEnd.closed(1006, "") == WatchDropped("closed with code 1006")
    assert WatchEnd.closed(1000, "") == WatchDropped("closed with code 1000")
    assert WatchEnd.closed(None, "").exit_code == 1
    assert WatchEnd.closed(WATCH_REPLACED, "") == WatchReplaced(f"closed with code {WATCH_REPLACED}")
    assert "start this again" in WatchEnd.closed(1006, "").line()
    assert "start this again" in WatchEnd.closed(WATCH_REPLACED, "").line()
    # The connector's own reason rides along, so a 1008 says which refusal it was.
    assert "code 1008: Another live process" in WatchEnd.closed(
        1008, ": Another live process owns this CLI endpoint.").line()


async def test_a_replaced_watch_never_reconnects_to_take_its_place_back(tmp_path: Path) -> None:
    """Two clients reconnecting over each other would replace each other for ever."""
    replaced = WatchEnd.closed(WATCH_REPLACED, "")
    assert await replaced.settled(InboxSocket("ws://unused/watch/x", tmp_path)) == replaced


async def reconnect_url(socket) -> str:
    """What a client that says it reconnects alone is handed; the path this test client opens."""
    await socket.send_str(ReconnectsAlone(printed=()).line())
    line = await asyncio.wait_for(socket.receive_str(), timeout=30)
    assert line.startswith(ReconnectUrl.wire_prefix)
    return URL(ReconnectUrl.model_validate_json(line).url).path


async def test_only_a_client_that_says_it_reconnects_is_handed_a_reconnect_url(
    session, comms: Comms,
) -> None:
    """An older client decodes every frame as a message, so it must never get anything else."""
    client, token = session
    older = await client.ws_connect(await watch_path(client, token, "cloud"))
    comms.messaging.send("sender", "cloud", "only messages")
    assert (await frame(older))["text"] == "only messages"
    await older.close()

    newer = await client.ws_connect(await watch_path(client, token, "cloud"))
    assert URL(await reconnect_url(newer)).parent.name == "watch"
    await newer.close()


async def test_a_redialled_socket_holds_its_next_url_before_anything_else(
    session, comms: Comms,
) -> None:
    """Resets come in bursts: on 2026-11-05 at 21:51 a re-dialled socket dropped 8 s after it
    opened, while its next URL still waited on the transcript reader. A socket opened with a
    reconnect ticket is sent that URL first, unasked, ahead of a message already waiting."""
    client, token = session
    first = await client.ws_connect(await watch_path(client, token, "cloud"))
    path = await reconnect_url(first)
    await first.close()
    comms.messaging.send("sender", "cloud", "waiting at the re-dial")
    again = await client.ws_connect(path)
    line = await asyncio.wait_for(again.receive_str(), timeout=30)
    assert line.startswith(ReconnectUrl.wire_prefix)
    following = URL(ReconnectUrl.model_validate_json(line).url).path
    assert (await frame(again))["text"] == "waiting at the re-dial"
    await again.close()
    # The URL it was handed first is the socket's own: re-dialling it opens.
    third = await client.ws_connect(following)
    await third.close()


async def test_a_reconnect_url_opens_once_and_a_newer_watch_ends_it(session, comms: Comms) -> None:
    """It has no clock, so only redemption or a newer watch of its thread ends it."""
    client, token = session
    first = await client.ws_connect(await watch_path(client, token, "cloud"))
    path = await reconnect_url(first)
    await first.close()
    again = await client.ws_connect(path)
    opened_with = await asyncio.wait_for(again.receive_str(), timeout=30)
    assert opened_with.startswith(ReconnectUrl.wire_prefix)
    with pytest.raises(WSServerHandshakeError):
        await client.ws_connect(path)
    comms.messaging.send("sender", "cloud", "after the reconnect")
    assert (await frame(again))["text"] == "after the reconnect"

    # Asked again, the socket answers with the URL it opened with, never a second mint.
    standing = await reconnect_url(again)
    assert standing == URL(ReconnectUrl.model_validate_json(opened_with).url).path
    rearmed = await client.ws_connect(await watch_path(client, token, "cloud"))
    with pytest.raises(WSServerHandshakeError):
        await client.ws_connect(standing)
    await again.close()
    await rearmed.close()


async def test_a_reconnect_url_outlives_the_connector_that_minted_it(
    tmp_path: Path, comms: Comms,
) -> None:
    """Every install restarts the connector, and a reset client re-dials into the new process:
    at 03:15 on 2026-11-06 three cloud watches were refused across one install, because their
    tickets lived in the old process's memory. The ticket is the connector's own fact, kept in
    its store, so the next process honours it once, and it never authorizes an MCP call."""
    comms.registry.declare(ExternalThread("claude-ai", frozenset(), Checkout(comms.root)))
    store = ConnectorStore.at(tmp_path / "auth")
    port = unused_port()
    issuer = f"http://127.0.0.1:{port}"
    with store.granted("test") as token:
        async with TestServer(Connector(store, issuer, comms.root).application(), port=port) as server:
            async with TestClient(server) as client:
                first = await client.ws_connect(await watch_path(client, token, "cloud"))
                path = await reconnect_url(first)
                await first.close()
        with pytest.raises(GrantRefusedError):
            store.read().caller_for(URL(path).name)
        async with TestServer(Connector(store, issuer, comms.root).application(), port=port) as server:
            async with TestClient(server) as client:
                again = await client.ws_connect(path)
                opened_with = await asyncio.wait_for(again.receive_str(), timeout=30)
                assert opened_with.startswith(ReconnectUrl.wire_prefix)
                with pytest.raises(WSServerHandshakeError):
                    await client.ws_connect(path)
                standing = await reconnect_url(again)
                await again.close()
                # Revoking the thread ends its watch's reconnect URL with its grant.
                store.thread_revoked("cloud")
                with pytest.raises(WSServerHandshakeError):
                    await client.ws_connect(standing)


async def test_a_reconnect_into_maintenance_is_deferred_and_keeps_its_next_url(
    session, comms: Comms,
) -> None:
    """Every install holds maintenance, and a reset client re-dials into it: on 2026-11-06 the
    connector refused each re-dial as a policy violation (1008), and the client spent its five
    resets in seconds. The refusal now says to wait, and the URL handed over first opens once
    maintenance lifts."""
    assert isinstance(WatchEnd.closed(WATCH_DEFERRED, ""), WatchDeferred)
    client, token = session
    first = await client.ws_connect(await watch_path(client, token, "cloud"))
    path = await reconnect_url(first)
    await first.close()
    barrier = MaintenanceBarrier(RegistryFile.path_in(comms.root))
    held = barrier.begin(MaintenanceOperator.current())
    try:
        deferred = await client.ws_connect(path)
        handed = await asyncio.wait_for(deferred.receive_str(), timeout=30)
        assert handed.startswith(ReconnectUrl.wire_prefix)
        closing = await asyncio.wait_for(deferred.receive(), timeout=30)
        assert closing.type is WSMsgType.CLOSE and closing.data == WATCH_DEFERRED, closing
    finally:
        barrier.clear(held)
    again = await client.ws_connect(URL(ReconnectUrl.model_validate_json(handed).url).path)
    # A reconnect ticket's socket holds its next URL before any message.
    assert (await asyncio.wait_for(again.receive_str(), timeout=30)).startswith(ReconnectUrl.wire_prefix)
    comms.messaging.send("sender", "cloud", "after the maintenance")
    assert (await frame(again))["text"] == "after the maintenance"
    await again.close()


class QuickRedials(Redials):
    """Millisecond pauses, so a wait past the old run's nine takes no time here."""

    pauses = Pauses.of((0.01,))
    first_wait = Pause(0.01, pauses)
    still_held = Pause(0.01, pauses)
    restarting = Pause(0.01, pauses)


@dataclass(frozen=True)
class CountedPause(Pause):
    """A pause that counts the re-dials it makes, which a re-dial no longer prints."""

    dials: list[str] = field(default_factory=list)

    async def retried(self, watch: InboxSocket, url: str, ended: WatchDropped) -> WatchEnd:
        self.dials.append(url)
        return await super().retried(watch, url, ended)


class CountedMaintenanceRedials(QuickRedials):
    still_held = CountedPause(0.01, QuickRedials.pauses)


async def test_a_watch_waits_out_maintenance_however_long_it_holds(
    session, comms: Comms, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Tonight's install held maintenance for 5 min 16 s, past the 3.5 min wait first written for
    it. A deferral is the connector, alive, saying maintenance still holds, so the real client
    waits on for as long as that is the answer, says so once, and is watching when it lifts."""
    client, token = session
    first = await client.ws_connect(await watch_path(client, token, "cloud"))
    path = await reconnect_url(first)
    await first.close()
    barrier = MaintenanceBarrier(RegistryFile.path_in(comms.root))
    held = barrier.begin(MaintenanceOperator.current())
    watch = InboxSocket(str(client.make_url(path)), tmp_path / "exports" / "messages",
                        redials=CountedMaintenanceRedials())
    dials = CountedMaintenanceRedials.still_held.dials
    before = len(dials)
    running = asyncio.create_task(watch.carried())
    printed = ""
    try:
        try:
            async with asyncio.timeout(20):
                while len(dials) - before < 12:
                    if running.done():
                        pytest.fail(f"The watch gave up during maintenance: {printed}")
                    await asyncio.sleep(0.05)
                    printed += capsys.readouterr().out
        finally:
            barrier.clear(held)
        comms.messaging.send("sender", "cloud", "after a long maintenance")
        async with asyncio.timeout(20):
            while "after a long maintenance" not in printed:
                await asyncio.sleep(0.05)
                printed += capsys.readouterr().out
        assert printed.count(WAITS_FOR_MAINTENANCE) == 1, printed
        assert printed.count(WATCHING) == 1, printed
    finally:
        running.cancel()


async def test_a_watch_waits_out_a_restarting_connector(
    tmp_path: Path, comms: Comms, capsys: pytest.CaptureFixture[str],
) -> None:
    """Every install restarts the connector, and a reset client re-dials while the proxy in front
    of it answers 502: on 2026-11-06 claude-f-ci's watch met 502s past the 15 s unopened run and
    gave up. Nothing listening is not a refusal, so the unspent URL is dialled again until the
    connector is back, and the watch carries on."""
    comms.registry.declare(ExternalThread("claude-ai", frozenset(), Checkout(comms.root)))
    comms.registry.declare(NativeThread("sender", frozenset(), Checkout(comms.root)))
    store = ConnectorStore.at(tmp_path / "auth")
    port = unused_port()
    connector = Connector(store, f"http://127.0.0.1:{port}", comms.root)
    restarting = asyncio.Event()
    unreachable: list[str] = []

    @web.middleware
    async def proxy(request: web.Request, handler):
        """What the proxy answers while nothing listens behind it."""
        if restarting.is_set() and request.path.startswith("/watch/"):
            unreachable.append(request.path)
            raise web.HTTPBadGateway()
        return await handler(request)

    application = connector.application()
    application.middlewares.append(proxy)
    async with TestServer(application, port=port) as server, TestClient(server) as client:
        with store.granted("test") as token:
            watch = InboxSocket(str(client.make_url(await watch_path(client, token, "cloud"))),
                                tmp_path / "exports" / "messages", redials=QuickRedials())
            running = asyncio.create_task(watch.carried())
            printed = ""
            try:
                async with asyncio.timeout(20):
                    while RECONNECTS_ALONE not in printed:
                        await asyncio.sleep(0.05)
                        printed += capsys.readouterr().out
                restarting.set()
                await connector.open_viewers[0].viewer.close()
                async with asyncio.timeout(20):
                    while len(unreachable) < 8:
                        if running.done():
                            pytest.fail(f"The watch gave up while the connector restarted: {printed}")
                        await asyncio.sleep(0.05)
                        printed += capsys.readouterr().out
                restarting.clear()
                comms.messaging.send("sender", "cloud", "after the restart")
                async with asyncio.timeout(20):
                    while "after the restart" not in printed:
                        await asyncio.sleep(0.05)
                        printed += capsys.readouterr().out
            finally:
                running.cancel()


async def test_a_stopping_connector_ends_a_watch_handler_that_never_sees_the_close(
    tmp_path: Path, comms: Comms,
) -> None:
    """On 2026-11-06 the install's stop waited 60.07 s, aiohttp's shutdown_timeout to the
    decimal, on one watch handler still publishing its transcript: the close never reached it,
    and every watch's re-dial met 502 for that minute. A watch now ends its own handler."""
    comms.registry.declare(ExternalThread("claude-ai", frozenset(), Checkout(comms.root)))
    store = ConnectorStore.at(tmp_path / "auth")
    port = unused_port()
    connector = Connector(store, f"http://127.0.0.1:{port}", comms.root)
    async with TestServer(connector.application(), port=port) as server:
        async with TestClient(server) as client:
            with store.granted("test") as token:
                socket = await client.ws_connect(await watch_path(client, token, "cloud"))
                held, = connector.open_viewers
                busy = asyncio.create_task(asyncio.Event().wait())
                await OpenWatch(thread="cloud", viewer=held.viewer, handler=busy).ended()
                await asyncio.sleep(0)
                assert busy.cancelled()
                assert held.viewer.closed
                await socket.close()


def attempt_outcome(comms: Comms, message) -> str:
    with comms.registry.store.coordinator_reader.reader_snapshot() as db:
        attempt, = HarnessDelivery.select(db, message_id=message.message_id)
    return attempt.outcome.declared_name


async def test_a_pushed_message_is_delivered_only_once_the_transcript_shows_it(
    session, comms: Comms,
) -> None:
    """A frozen watch past its heartbeat took message 62380 and never showed it; the bus said
    delivered because the socket write returned. The write proves nothing: only the session's
    own Monitor event does, and a mangled one proves nothing either."""
    client, token = session
    socket = await client.ws_connect(await watch_path(client, token, "cloud"))
    shown = comms.messaging.send("sender", "cloud", "shown as written").message
    mangled = comms.messaging.send("sender", "cloud", "shown wrong").message
    wrapped = comms.messaging.send("sender", "cloud", "word " * 240 + "\n\nafter a blank line").message
    for _ in range(3):
        await frame(socket)
    await inbox_emptied(comms, "cloud")
    assert {attempt_outcome(comms, message) for message in (shown, mangled, wrapped)} == {"unconfirmed"}

    await socket.send_str(monitor_event_record(ClaudeMonitorFrame(shown).shown_inline()))
    await socket.send_str(monitor_event_record(
        ClaudeMonitorFrame(mangled).shown_inline().replace("wrong", "otherwise")))
    await socket.send_str(monitor_event_record(ClaudeMonitorFrame(wrapped).shown_inline()))
    async with asyncio.timeout(20):
        while {attempt_outcome(comms, shown), attempt_outcome(comms, wrapped)} != {"delivered"}:
            await asyncio.sleep(0.05)
    assert attempt_outcome(comms, mangled) == "unconfirmed"
    await socket.close()


TOOK_IT = PrintLedgerId("the ledger that took it")


def ledger_line(*messages, ledger: PrintLedgerId = TOOK_IT) -> str:
    return PrintedLedger(ledger=ledger, printed=tuple(message.reference for message in messages)).line()


async def ledger_reported(socket, client: TestClient, token: str, words: str) -> None:
    """The reader takes the socket's frames in order: once a line sent after the ledger shows
    as the session's work, the ledger was handled before it."""
    await socket.send_str(ledger_line())
    for line in transcript_lines(words):
        await socket.send_str(line)
    async with asyncio.timeout(20):
        while words not in await turns_of(client, token, "cloud"):
            await asyncio.sleep(0.05)


async def bridged(comms: Comms, thread: str) -> None:
    async with asyncio.timeout(20):
        while not isinstance(comms.registry.snapshot().entry(thread).status, ConnectedThreadStatus):
            await asyncio.sleep(0.05)


async def outcome_becomes(comms: Comms, message, outcome: str) -> None:
    async with asyncio.timeout(20):
        while attempt_outcome(comms, message) != outcome:
            await asyncio.sleep(0.05)


async def test_a_message_the_client_printed_reads_emitted(session, comms: Comms, tmp_path: Path, capsys) -> None:
    client, token = session
    url = str(client.make_url(await watch_path(client, token, "cloud")))
    source = tmp_path / "session-a.jsonl"
    source.write_text("".join(f"{line}\n" for line in transcript_lines("before")))
    watch = asyncio.create_task(
        InboxSocket(url, tmp_path / "cloud" / "messages", FollowedTranscript(source)).carried())
    await bridged(comms, "cloud")
    message = comms.messaging.send("sender", "cloud", "printed here").message
    await inbox_emptied(comms, "cloud")
    await outcome_becomes(comms, message, "emitted")
    ledgers = tmp_path / "cloud" / "printed"
    assert sorted(path.name for path in ledgers.iterdir()) == ["session-a", "session-a.id"]
    assert (ledgers / "session-a").read_text().split() == [message.reference.name]
    watch.cancel()


async def test_a_message_taken_but_never_printed_is_replayed_once_the_ledger_proves_it(
    session, comms: Comms,
) -> None:
    """62380: a frozen watch took it, the socket died, and it was gone for good."""
    client, token = session
    frozen = await client.ws_connect(await watch_path(client, token, "cloud"))
    await bridged(comms, "cloud")
    await ledger_reported(frozen, client, token, "ledger in before the freeze")
    lost = comms.messaging.send("sender", "cloud", "taken, never printed").message
    shown = comms.messaging.send("sender", "cloud", "printed before the freeze").message
    assert [(await frame(frozen))["text"] for _ in range(2)] == [lost.body, shown.body]
    await inbox_emptied(comms, "cloud")
    await frozen.close()

    again = await client.ws_connect(await watch_path(client, token, "cloud"))
    await again.send_str(ledger_line(shown))
    assert (await frame(again))["text"] == lost.body
    await outcome_becomes(comms, shown, "emitted")
    assert attempt_outcome(comms, lost) == "unconfirmed"
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(again.receive_str(), timeout=1)
    await again.close()


async def test_a_fresh_containers_empty_ledger_proves_nothing(session, comms: Comms) -> None:
    """The container that printed it was reclaimed; the session came back with an empty ledger
    under the same session id. That ledger never took the attempt, so its silence is no proof."""
    client, token = session
    reclaimed = await client.ws_connect(await watch_path(client, token, "cloud"))
    await bridged(comms, "cloud")
    await ledger_reported(reclaimed, client, token, "ledger in before the reclaim")
    comms.messaging.send("sender", "cloud", "printed in the reclaimed container")
    await frame(reclaimed)
    await inbox_emptied(comms, "cloud")
    await reclaimed.close()
    fresh = await client.ws_connect(await watch_path(client, token, "cloud"))
    await fresh.send_str(ledger_line(ledger=PrintLedgerId("born in the fresh container")))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(fresh.receive_str(), timeout=2)
    await fresh.close()


async def test_no_ledger_is_no_proof_so_nothing_is_replayed(session, comms: Comms) -> None:
    client, token = session
    old = await client.ws_connect(await watch_path(client, token, "cloud"))
    await bridged(comms, "cloud")
    comms.messaging.send("sender", "cloud", "taken by an old client")
    await frame(old)
    await inbox_emptied(comms, "cloud")
    await old.close()
    again = await client.ws_connect(await watch_path(client, token, "cloud"))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(again.receive_str(), timeout=2)
    await again.close()


@pytest.mark.parametrize("connector_type", [QuickHeartbeatConnector])
async def test_a_watch_frozen_past_its_heartbeat_reconnects_alone_and_shows_the_message_once(
    session, comms: Comms, tmp_path: Path,
) -> None:
    """The 62380 freeze with a real client process and a real SIGSTOP: the connector drops the
    stopped socket at its heartbeat while the message it took sits unshown. After SIGCONT the
    client re-dials with its reconnect URL, with no comms_watch from the model, and its ledger
    report makes the new socket send exactly what it did not print."""
    client, token = session
    url = str(client.make_url(await watch_path(client, token, "cloud")))
    (tmp_path / ".claude" / "projects" / "p").mkdir(parents=True)
    (tmp_path / ".claude" / "projects" / "p" / "frozen-session.jsonl").write_text(
        "".join(f"{line}\n" for line in transcript_lines("before the freeze")))
    environment = {**os.environ, "HOME": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "frozen-session",
                   "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    watch = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "from agent_comms.cli_bridges.inbox_watch_client import main; main()", url,
        env=environment, stdout=asyncio.subprocess.PIPE)
    assert watch.stdout is not None
    async with asyncio.timeout(30):
        assert b"Watching your inbox" in await watch.stdout.readline()
        # Frozen before the reconnect URL arrives, it would hold nothing to re-dial with.
        assert RECONNECTS_ALONE.encode() in await watch.stdout.readline()
    os.kill(watch.pid, signal.SIGSTOP)
    try:
        message = comms.messaging.send("sender", "cloud", "sent into the freeze").message
        await inbox_emptied(comms, "cloud")
        async with asyncio.timeout(90):
            while isinstance(comms.registry.snapshot().entry("cloud").status, ConnectedThreadStatus):
                await asyncio.sleep(0.5)
    finally:
        os.kill(watch.pid, signal.SIGCONT)
    printed: list[str] = []

    async def next_line() -> None:
        """An exited client reads as EOF at once, which must fail rather than spin."""
        line = await watch.stdout.readline()
        assert line, f"the client exited instead of reconnecting: {printed}"
        printed.append(line.decode().rstrip("\n"))

    try:
        async with asyncio.timeout(60):
            while not any(message.body in line for line in printed):
                await next_line()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(3):
                while True:
                    await next_line()
    finally:
        watch.terminate()
        await watch.wait()
    assert sum(line == message.body for line in printed) == 1, printed
    # The message is all the reconnect prints: the session was told before the freeze that the
    # watch is up and reconnects alone, and each printed line costs it a turn.
    assert not any(line in (WATCHING, RECONNECTS_ALONE) or "reconnecting" in line
                   for line in printed), printed
    assert not any("/watch/" in line for line in printed), printed


def read_records(path: str, text: str) -> tuple[str, str]:
    """The transcript records Claude writes for one Read call and its visible result."""
    call = str(uuid4())
    started = {"type": "assistant", "uuid": str(uuid4()), "timestamp": "2026-11-04T00:00:01.000Z",
               "sessionId": "s", "message": {"content": [
                   {"type": "tool_use", "id": call, "name": "Read", "input": {"file_path": path}}]}}
    returned = {"type": "user", "uuid": str(uuid4()), "timestamp": "2026-11-04T00:00:02.000Z",
                "sessionId": "s", "promptId": "p", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": call, "content": text, "is_error": False}]}}
    return json.dumps(started), json.dumps(returned)


async def test_a_cloud_message_sent_as_a_file_is_delivered_once_its_read_shows_it(
    session, comms: Comms,
) -> None:
    """A message too long for one event goes to a file on the session's machine. Its Read comes
    up the socket in the transcript, and the lines are judged against the bus message, so a
    Read of the right name with other lines confirms nothing."""
    client, token = session
    socket = await client.ws_connect(await watch_path(client, token, "cloud"))
    body = "\n".join(f"line {number} " + "x" * 60 for number in range(80))
    whole = comms.messaging.send("sender", "cloud", body).message
    forged = comms.messaging.send("sender", "cloud", body.replace("line 7 ", "line 7?")).message
    for _ in range(2):
        await frame(socket)
    await inbox_emptied(comms, "cloud")
    assert not ClaudeMonitorFrame(whole).fits_one_event

    def numbered(lines: tuple[str, ...]) -> str:
        return "\n".join(f"{number}\t{line}" for number, line in enumerate(lines, 1))

    for message, lines in ((whole, ClaudeMonitorFrame(whole).export_lines),
                           (forged, ClaudeMonitorFrame(whole).export_lines)):
        path = f"/elsewhere/state/messages/{message.reference.name}.txt"
        for record in read_records(path, numbered(lines)):
            await socket.send_str(record)
    async with asyncio.timeout(20):
        while attempt_outcome(comms, whole) != "delivered":
            await asyncio.sleep(0.05)
    assert attempt_outcome(comms, forged) == "unconfirmed"
    await socket.close()


def cat_records(path: str, text: str) -> tuple[str, str]:
    """The transcript records Claude writes for a Bash `cat` of one file and its output."""
    started, returned = map(json.loads, read_records(path, text))
    started["message"]["content"][0].update(name="Bash", input={"command": f"cat {path}"})
    return json.dumps(started), json.dumps(returned)


async def test_a_cloud_message_sent_as_a_file_is_delivered_once_a_cat_shows_it(
    session, comms: Comms,
) -> None:
    """A session that reads its messages with Bash cat, as auto mode suggests, put every file
    sent message in reserve forever (claude-g-standby). The cat output is the file as it is,
    judged against the bus message like a Read; a cat of an edited copy confirms nothing."""
    client, token = session
    socket = await client.ws_connect(await watch_path(client, token, "cloud"))
    body = "\n".join(f"line {number} " + "x" * 60 for number in range(80))
    whole = comms.messaging.send("sender", "cloud", body).message
    edited = comms.messaging.send("sender", "cloud", body.replace("line 7 ", "line 7?")).message
    for _ in range(2):
        await frame(socket)
    await inbox_emptied(comms, "cloud")
    for message in (whole, edited):
        path = f"/elsewhere/state/messages/{message.reference.name}.txt"
        for record in cat_records(path, "\n".join(ClaudeMonitorFrame(whole).export_lines)):
            await socket.send_str(record)
    async with asyncio.timeout(20):
        while attempt_outcome(comms, whole) != "delivered":
            await asyncio.sleep(0.05)
    assert attempt_outcome(comms, edited) == "unconfirmed"
    await socket.close()
