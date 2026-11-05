"""A session on another machine is woken by a pushed frame, through the login it already has."""

import asyncio
import os
import signal
import sys
import json
import re
from pathlib import Path
from uuid import uuid4

import pytest
from aiohttp import WSServerHandshakeError
from pydantic import ValidationError
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from agent_comms.activity import ActivityState, ObservedActivity
from agent_comms.cli_bridges.claude_protocol import ClaudeMonitorFrame
from agent_comms.cli_bridges.claude_protocol import MonitorTaskNotification, TaskEnvelope
from agent_comms.cli_bridges.claude_transcript import WATCH_REPLACED, PrintedLedger
from agent_comms.harness_delivery_states import PrintLedgerId
from agent_comms.cli_bridges.inbox_watch_client import (
    FollowedTranscript, InboxSocket, WatchDropped, WatchEnd, WatchStopped,
)
from agent_comms.comms import Comms
from agent_comms.channel_targets import CommandTarget
from agent_comms.connector import Connector
from agent_comms.connector_store import ConnectorStore
from agent_comms.message_reference import MessageReference
from agent_comms.coordination.tables.harness_deliveries import HarnessDelivery
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
    assert lines[start].startswith("Comms in · sender (external agent) · ")
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
    assert written.startswith("Comms in · sender (external agent) · ")
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
    assert WatchEnd.closed(1006) == WatchDropped("closed with code 1006")
    assert WatchEnd.closed(1000) == WatchDropped("closed with code 1000")
    assert WatchEnd.closed(None).exit_code == 1
    assert WatchEnd.closed(WATCH_REPLACED) == WatchDropped(f"closed with code {WATCH_REPLACED}")
    assert "start this again" in WatchEnd.closed(1006).line()


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


async def test_a_watch_frozen_past_its_heartbeat_loses_nothing_and_shows_nothing_twice(
    session, comms: Comms, tmp_path: Path,
) -> None:
    """The 62380 freeze with a real client process and a real SIGSTOP: the connector drops the
    stopped socket at its heartbeat while the message it took sits unshown. Whatever the client
    prints after SIGCONT is in its ledger, and the next watch sends exactly what it did not."""
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
    os.kill(watch.pid, signal.SIGSTOP)
    try:
        message = comms.messaging.send("sender", "cloud", "sent into the freeze").message
        await inbox_emptied(comms, "cloud")
        async with asyncio.timeout(90):
            while isinstance(comms.registry.snapshot().entry("cloud").status, ConnectedThreadStatus):
                await asyncio.sleep(0.5)
    finally:
        os.kill(watch.pid, signal.SIGCONT)
    async with asyncio.timeout(30):
        printed = (await watch.stdout.read()).decode()
    await watch.wait()
    ledger = tmp_path / ".local" / "state" / "claude-comms-inbox" / "cloud" / "printed" / "frozen-session"
    names = ledger.read_text().split() if ledger.exists() else []
    assert (message.body in printed) == (message.reference.name in names)

    again = await client.ws_connect(await watch_path(client, token, "cloud"))
    identity = PrintLedgerId(ledger.with_suffix(".id").read_text())
    await again.send_str(PrintedLedger(
        ledger=identity, printed=tuple(MessageReference.named(n) for n in names)).line())
    if message.reference.name in names:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(again.receive_str(), timeout=2)
    else:
        assert (await frame(again))["text"] == message.body
    await again.close()


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
