"""Antigravity harness installation, transcript decoding, and live inbox-watch streaming."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer, unused_port
from yarl import URL

from agent_comms.cli_bridges.antigravity import AntigravityBridge
from agent_comms.cli_bridges.antigravity_rule_gate import (
    AntigravityReadFileTool,
    handle_hook,
)
from agent_comms.cli_bridges.antigravity_transcript import (
    AntigravityTranscript,
    RunningAntigravity,
)
from agent_comms_hooks.antigravity import (
    ANTIGRAVITY_HOOK_NAME,
    _fast_path as agy_hook_fast_path,
)
from agent_comms_hooks.hook_switch import StartVerb, StopVerb
from agent_comms_hooks.skill import DeckReader
from agent_comms.cli_bridges.history import OfflineHistory, OnlineHistory
from agent_comms.cli_bridges.inbox_watch_client import (
    AntigravityFollowedTranscript,
    FollowedTranscript,
    InboxSocket,
    session_transcript,
    wait_for_log_growth,
)
from agent_comms.cli_bridges.harness_delivery_states import (
    AcknowledgedReceipt,
    DeliveredOutcome,
    EmittedOutcome,
    ObservedMonitor,
)
from agent_comms.cli_bridges.history import RemoteWatchHistory
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.tables.harness_deliveries import HarnessDelivery
from agent_comms.messaging.notification_evidence import AllOutcomes
from agent_comms.messaging.notification_progress import AnsweredResponse, HarnessConsumedProgress
from agent_comms.nominal.field_codec import TranscriptRecordId
from agent_comms.cli_bridges.runtime_updates import (
    FinishedItem,
    RuntimeInfoObserved,
    SessionFacts,
    Summarized,
)
from agent_comms.comms import Comms
from agent_comms.installer.harness_install import (
    AntigravityHarness,
    ConnectorClientHarness,
    Harness,
)
from agent_comms.pi_counts import ReportedCount
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.activity import ActivityState
from agent_comms.registry.thread_text import AnySession
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.transcript.transcript_events import (
    AssistantTranscript,
    ThinkingTranscript,
    ToolEndTranscript,
    ToolStartTranscript,
    UserTranscript,
)
from agent_comms.transport.connector import Connector
from agent_comms.transport.connector_store import ConnectorStore, ReadServiceAccess


def _sample_antigravity_lines() -> list[str]:
    return [
        json.dumps({
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-23T18:00:00Z",
            "content": "<USER_REQUEST>\nFix the bus bridge friction\n</USER_REQUEST>",
        }),
        json.dumps({
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-23T18:00:01Z",
            "model": "gemini-4-neon-high",
            "input_tokens": 1200,
            "cache_read_tokens": 3800,
            "output_tokens": 180,
            "thinking": "Tracing harness_install.py to add AntigravityHarness.",
            "tool_calls": [
                {"name": "run_command", "args": {"CommandLine": "pytest -q tests/test_antigravity_harness.py"}}
            ],
        }),
        json.dumps({
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "RUNNING",
            "created_at": "2026-11-23T18:00:02Z",
            "content": "Tool is running as a background task",
        }),
        json.dumps({
            "step_index": 3,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": "2026-11-23T18:00:05Z",
            "content": "4 passed in 1.2s",
        }),
        json.dumps({
            "step_index": 4,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "created_at": "2026-11-23T18:00:06Z",
            "content": "Internal reminder",
        }),
        json.dumps({
            "step_index": 5,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-23T18:00:07Z",
            "model": "gemini-4-neon-high",
            "input_tokens": 1500,
            "cache_read_tokens": 4100,
            "output_tokens": 90,
            "content": "Added AntigravityHarness and live transcript streaming.",
        }),
    ]


def test_antigravity_harness_registers_mcp_connector_and_links_skills(tmp_path: Path) -> None:
    assert AntigravityHarness in Harness.members_with(ConnectorClientHarness)
    harness = AntigravityHarness(tmp_path)

    links = harness.links()
    assert links
    assert all(link.parent == tmp_path / ".gemini/config/skills" for link in links)

    mcp_path = tmp_path / ".gemini/config/mcp_config.json"
    mcp_path.parent.mkdir(parents=True)
    # Antigravity may create a 0-byte mcp_config.json before any MCP server is configured.
    mcp_path.write_text("", encoding="utf-8")
    assert not harness.connector_registered()
    harness.register_connector("http://127.0.0.1:41973/mcp", "tok-empty")
    assert harness.connector_registered()

    mcp_path.write_text(json.dumps({"mcpServers": {"existing": {"command": "foo"}}}))
    assert not harness.connector_registered()

    harness.register_connector("http://127.0.0.1:41973/mcp", "tok-123")
    assert harness.connector_registered()
    saved = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert saved["mcpServers"]["existing"] == {"command": "foo"}
    assert saved["mcpServers"]["comms"] == {
        "url": "http://127.0.0.1:41973/mcp",
        "headers": {"Authorization": "Bearer tok-123"},
    }


def test_antigravity_offline_and_online_history_decode_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv_id = "ece01b7c-5186-4622-b71f-6a63198f1755"
    transcript = (
        tmp_path
        / ".gemini/antigravity-cli/brain"
        / conv_id
        / ".system_generated/logs/transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    lines = [
        json.dumps({
            "step_index": 0,
            "source": "SYSTEM",
            "type": "CHECKPOINT",
            "status": "DONE",
            "created_at": "2026-11-23T17:59:59Z",
            "content": "Prior summary",
        }),
        *_sample_antigravity_lines(),
        json.dumps({
            "step_index": 6,
            "source": "MODEL",
            "type": "FUTURE_STEP_KIND",
            "status": "DONE",
            "created_at": "2026-11-23T18:00:08Z",
        }),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reader = OfflineHistory.stored(AntigravityHarness, transcript, AnySession())
    assert isinstance(reader, AntigravityTranscript)
    updates = list(reader.read())

    facts = [u for u in updates if isinstance(u, SessionFacts)]
    assert facts and facts[0].source_id == conv_id
    assert any(isinstance(u, Summarized) and u.text == "Prior summary" for u in updates)
    events = [event for u in updates if isinstance(u, FinishedItem) for event in u.events]
    assert any(isinstance(e, UserTranscript) and e.text == "Fix the bus bridge friction" for e in events)
    assert any(isinstance(e, ThinkingTranscript) for e in events)
    assert any(isinstance(e, ToolStartTranscript) and e.tool_name == "run_command" for e in events)
    assert any(isinstance(e, ToolEndTranscript) and e.ok and "4 passed" in e.text for e in events)
    assert any(
        isinstance(e, AssistantTranscript) and "Added AntigravityHarness" in e.text for e in events
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTIGRAVITY_TRANSCRIPT", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID", raising=False)
    running = OnlineHistory.running(AntigravityHarness, conv_id, None, tmp_path)  # type: ignore[arg-type]
    assert isinstance(running, RunningAntigravity)
    assert running.source == transcript
    assert AntigravityBridge.environment_session() == conv_id


def test_session_transcript_auto_discovers_antigravity_and_keeps_claude_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv_id = "11111111-2222-3333-4444-555555555555"
    agy_path = (
        tmp_path
        / ".gemini/antigravity-cli/brain"
        / conv_id
        / ".system_generated/logs/transcript.jsonl"
    )
    agy_path.parent.mkdir(parents=True)
    agy_path.write_text(_sample_antigravity_lines()[0] + "\n", encoding="utf-8")

    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_TRANSCRIPT", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID", raising=False)

    route = session_transcript(home=tmp_path)
    assert isinstance(route, AntigravityFollowedTranscript)
    assert route.source == agy_path
    assert route.ledger_name() == conv_id

    explicit = session_transcript(transcript=agy_path)
    assert isinstance(explicit, AntigravityFollowedTranscript)
    assert explicit.source == agy_path

    # When CLAUDE_CODE_SESSION_ID is exported, Claude remains selected first.
    claude_id = "claude-sess-99"
    claude_path = tmp_path / ".claude/projects/demo" / f"{claude_id}.jsonl"
    claude_path.parent.mkdir(parents=True)
    claude_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", claude_id)
    claude_route = session_transcript(home=tmp_path)
    assert isinstance(claude_route, FollowedTranscript)
    assert claude_route.source == claude_path


async def test_inbox_watch_streams_antigravity_transcript_and_live_activity(
    tmp_path: Path, comms: Comms
) -> None:
    comms.registry.declare(ExternalThread("claude-ai", frozenset(), Checkout(comms.root)))
    comms.registry.declare(NativeThread("sender", frozenset(), Checkout(comms.root)))
    store = ConnectorStore.at(tmp_path / "auth")
    port = unused_port()
    connector = Connector(store, f"http://127.0.0.1:{port}", comms.root)

    conv_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    transcript = (
        tmp_path
        / ".gemini/antigravity-cli/brain"
        / conv_id
        / ".system_generated/logs/transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    lines = _sample_antigravity_lines()
    # Write opening prompt + tool call (still running!) so live activity flips to working/tool.
    transcript.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")

    async with TestServer(connector.application(), port=port) as server:
        async with TestClient(server) as client:
            with store.granted("test") as token:
                resp = await client.post(
                    "/mcp",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "comms_watch", "arguments": {"fork": {"name": "agy-live"}}},
                    },
                )
                reply = await resp.json()
                assert not reply["result"]["isError"], reply
                url = str(client.make_url(URL(reply["result"]["structuredContent"]["url"]).path))

                watch = asyncio.create_task(
                    InboxSocket(
                        url,
                        tmp_path / "messages",
                        AntigravityFollowedTranscript(transcript),
                    ).carried()
                )
                try:
                    async with asyncio.timeout(15):
                        while True:
                            obs = comms.agents.activity_of(
                                "agy-live", snapshot=comms.registry.snapshot()
                            )
                            if obs.state is ActivityState.WORKING:
                                break
                            await asyncio.sleep(0.05)

                    # Append tool completion + final assistant reply -> turn finishes -> IDLE.
                    with transcript.open("a", encoding="utf-8") as fh:
                        fh.write("\n".join(lines[3:]) + "\n")

                    async with asyncio.timeout(15):
                        while True:
                            obs = comms.agents.activity_of(
                                "agy-live", snapshot=comms.registry.snapshot()
                            )
                            if obs.state is ActivityState.IDLE:
                                break
                            await asyncio.sleep(0.05)

                    # Confirm model runtime info was recorded from PLANNER_RESPONSE.
                    info = comms.agents.agent_info_of("agy-live")
                    assert "gemini-4-neon-high" in str(info.model)
                    assert info.context_used == ReportedCount(5600)
                finally:
                    watch.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await watch


async def test_antigravity_persistent_watch_confirms_pty_double_cr_sync_and_async_wake(
    tmp_path: Path, comms: Comms, capsys: pytest.CaptureFixture[str]
) -> None:
    comms.registry.declare(ExternalThread("claude-ai", frozenset(), Checkout(comms.root)))
    comms.registry.declare(NativeThread("sender", frozenset(), Checkout(comms.root)))
    store = ConnectorStore.at(tmp_path / "auth")
    port = unused_port()
    connector = Connector(store, f"http://127.0.0.1:{port}", comms.root)

    conv_id = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    transcript = (
        tmp_path
        / ".gemini/antigravity-cli/brain"
        / conv_id
        / ".system_generated/logs/transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(_sample_antigravity_lines()[0] + "\n", encoding="utf-8")

    # Event-driven wait_for_log_growth wakes on append and normalizes PTY \r\n -> \n.
    watch_log = tmp_path / "watch.log"
    watch_log.write_bytes(b"initial\r\n")
    waiter = asyncio.create_task(wait_for_log_growth(watch_log))
    await asyncio.sleep(0.1)
    with watch_log.open("ab") as fh:
        fh.write(b"line-one\r\r\nline-two\r\n")
    assert await asyncio.wait_for(waiter, timeout=5) == "line-one\nline-two\n"

    async with TestServer(connector.application(), port=port) as server:
        async with TestClient(server) as client:
            with store.granted("test") as token:
                resp = await client.post(
                    "/mcp",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "comms_watch", "arguments": {"fork": {"name": "agy-wake"}}},
                    },
                )
                reply = await resp.json()
                assert not reply["result"]["isError"], reply
                url = str(client.make_url(URL(reply["result"]["structuredContent"]["url"]).path))

                watch = asyncio.create_task(
                    InboxSocket(
                        url,
                        tmp_path / "messages",
                        AntigravityFollowedTranscript(transcript),
                    ).carried()
                )
                try:
                    await asyncio.sleep(0.2)
                    async_msg = comms.messaging.send("sender", "agy-wake", "async wake body").message
                    sync_msg = comms.messaging.send("sender", "agy-wake", "sync wake body").message

                    async with asyncio.timeout(15):
                        printed = ""
                        while (
                            f"{async_msg.seq}-{async_msg.message_id}" not in printed
                            or f"{sync_msg.seq}-{sync_msg.message_id}" not in printed
                        ):
                            printed += capsys.readouterr().out
                            await asyncio.sleep(0.05)

                    # Simulate double-CR PTY output in both SYSTEM_MESSAGE (async wake) and
                    # GENERIC (synchronous run_command completion) while the same watch stays open.
                    async_lines = [
                        line for line in printed.splitlines()
                        if f"{async_msg.seq}-{async_msg.message_id}" in line or line == "async wake body"
                    ]
                    sync_lines = [
                        line for line in printed.splitlines()
                        if f"{sync_msg.seq}-{sync_msg.message_id}" in line or line == "sync wake body"
                    ]
                    async_step = {
                        "step_index": 7,
                        "source": "SYSTEM",
                        "type": "SYSTEM_MESSAGE",
                        "status": "DONE",
                        "created_at": "2026-11-23T18:01:00Z",
                        "content": (
                            "<SYSTEM_MESSAGE>\nCommand exited with code 0.\n\n"
                            "Command output:\n"
                            + "\r\r\n".join(async_lines)
                            + "\r\r\n\nLog: file:///tmp/task-1.log\n</SYSTEM_MESSAGE>"
                        ),
                    }
                    sync_step = {
                        "step_index": 8,
                        "source": "MODEL",
                        "type": "GENERIC",
                        "status": "DONE",
                        "created_at": "2026-11-23T18:01:05Z",
                        "content": (
                            "Command exited with code 0.\n\n"
                            "Command output:\n"
                            + "\r\r\n".join(sync_lines)
                            + "\r\r\n"
                        ),
                    }
                    with transcript.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(async_step) + "\n")
                        fh.write(json.dumps(sync_step) + "\n")

                    lookup = comms.registry.require("agy-wake").incarnation.require_recorded().lookup
                    async with asyncio.timeout(15):
                        while True:
                            with comms.registry.store.coordinator_reader.reader_snapshot() as db:
                                r1 = HarnessDelivery.select(
                                    db, bridge=RemoteWatchHistory,
                                    recipient_lookup=lookup, message_id=async_msg.message_id,
                                )
                                r2 = HarnessDelivery.select(
                                    db, bridge=RemoteWatchHistory,
                                    recipient_lookup=lookup, message_id=sync_msg.message_id,
                                )
                            if (
                                any(row.outcome is DeliveredOutcome and isinstance(row.monitor, ObservedMonitor) for row in r1)
                                and any(row.outcome is DeliveredOutcome and isinstance(row.monitor, ObservedMonitor) for row in r2)
                            ):
                                break
                            await asyncio.sleep(0.05)
                    # Persistent watch never exited!
                    assert not watch.done()
                finally:
                    watch.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await watch


async def test_local_thread_grant_calls_comms_watch_and_binds_existing_external_thread(
    tmp_path: Path, comms: Comms
) -> None:
    comms.registry.declare(ExternalThread("antigravity", frozenset(), Checkout(comms.root)))
    comms.registry.declare(ExternalThread("agy-existing", frozenset({"model"}), Checkout(comms.root)))
    original = comms.registry.require("agy-existing")
    store = ConnectorStore.at(tmp_path / "auth")
    port = unused_port()
    connector = Connector(store, f"http://127.0.0.1:{port}", comms.root)
    token = store.thread_granted("antigravity")

    async with TestServer(connector.application(), port=port) as server:
        async with TestClient(server) as client:
            resp = await client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "comms_watch",
                        "arguments": {"fork": {"name": "agy-existing"}},
                    },
                },
            )
            reply = await resp.json()
            assert not reply["result"]["isError"], reply
            watch_url = reply["result"]["structuredContent"]["url"]
            assert "/watch/" in watch_url

            after = comms.registry.require("agy-existing")
            assert after.incarnation == original.incarnation
            assert not after.is_fork

            # Director-only connector tools remain hidden from local thread grants.
            director_only = await client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "comms_serve_model", "arguments": {}},
                },
            )
            director_reply = await director_only.json()
            assert director_reply["result"]["isError"]

            # Read-only grants still cannot open an inbox watch.
            with store.granted("readonly", grant_type=ReadServiceAccess) as ro_token:
                ro_resp = await client.post(
                    "/mcp",
                    headers={"Authorization": f"Bearer {ro_token}"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "comms_watch", "arguments": {}},
                    },
                )
                ro_reply = await ro_resp.json()
                assert ro_reply["result"]["isError"]


async def test_comms_ack_and_answered_send_settle_cloud_delivery_without_transcript(
    tmp_path: Path, comms: Comms
) -> None:
    comms.registry.declare(ExternalThread("antigravity", frozenset(), Checkout(comms.root)))
    comms.registry.declare(ExternalThread("agy-cloud", frozenset({"model"}), Checkout(comms.root)))
    comms.registry.declare(NativeThread("sender", frozenset(), Checkout(comms.root)))
    comms.registry.declare(ExternalThread("bystander", frozenset(), Checkout(comms.root)))

    first = comms.messaging.send("sender", "agy-cloud", "first cloud ping").message
    second = comms.messaging.send("sender", "agy-cloud", "second cloud ping").message
    third = comms.messaging.send("sender", "agy-cloud", "already confirmed by transcript").message
    other = comms.messaging.send("sender", "bystander", "not for agy-cloud").message

    lookup = comms.registry.require("agy-cloud").incarnation.require_recorded().lookup
    observed = ObservedMonitor(
        TranscriptRecordId("11111111-2222-3333-4444-555555555555"), 1795465000.0
    )
    with Coordination(str(CoordinationStore.path_in(comms.root))) as store:
        with store.session.writing() as db:
            HarnessDelivery(
                RemoteWatchHistory, lookup, first.message_id, first.seq, EmittedOutcome
            ).begin(store.assignments, db)
            HarnessDelivery(
                RemoteWatchHistory, lookup, second.message_id, second.seq, EmittedOutcome
            ).begin(store.assignments, db)
            HarnessDelivery(
                RemoteWatchHistory, lookup, third.message_id, third.seq, DeliveredOutcome,
                monitor=observed,
            ).record(store.assignments, db)

    before_first, = comms.views.message_notifications_for_references(
        AllOutcomes(), (first.reference,)
    ).outcomes[first.seq, first.message_id]
    assert not before_first.state.is_consumed()

    store = ConnectorStore.at(tmp_path / "auth")
    port = unused_port()
    connector = Connector(store, f"http://127.0.0.1:{port}", comms.root)
    token = store.thread_granted("antigravity")

    async with TestServer(connector.application(), port=port) as server:
        async with TestClient(server) as client:
            # 1) Explicit comms_ack flips EmittedOutcome -> DeliveredOutcome + AcknowledgedReceipt -> Read.
            ack_resp = await client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "comms_ack",
                        "arguments": {
                            "fork": {"name": "agy-cloud"},
                            "messages": [
                                {"seq": first.seq, "message_id": str(first.message_id)},
                                {"seq": third.seq, "message_id": str(third.message_id)},
                            ],
                        },
                    },
                },
            )
            ack_reply = await ack_resp.json()
            assert not ack_reply["result"]["isError"], ack_reply

            after_first, = comms.views.message_notifications_for_references(
                AllOutcomes(), (first.reference,)
            ).outcomes[first.seq, first.message_id]
            assert after_first.state.is_consumed()
            assert after_first.label.startswith(HarnessConsumedProgress.label)

            # Stronger transcript proof on `third` is preserved (never downgraded to AcknowledgedReceipt).
            with comms.registry.store.coordinator_reader.reader_snapshot() as db:
                first_row, = HarnessDelivery.select(
                    db, bridge=RemoteWatchHistory, recipient_lookup=lookup, message_id=first.message_id
                )
                third_row, = HarnessDelivery.select(
                    db, bridge=RemoteWatchHistory, recipient_lookup=lookup, message_id=third.message_id
                )
            assert first_row.outcome is DeliveredOutcome
            assert isinstance(first_row.monitor, AcknowledgedReceipt)
            assert third_row.outcome is DeliveredOutcome
            assert third_row.monitor == observed

            # 2) Refuses acknowledging a message this thread never received.
            bad_resp = await client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "comms_ack",
                        "arguments": {
                            "fork": {"name": "agy-cloud"},
                            "messages": [{"seq": other.seq, "message_id": str(other.message_id)}],
                        },
                    },
                },
            )
            bad_reply = await bad_resp.json()
            assert bad_reply["result"]["isError"]

            # 3) Replying via comms_send with answers settles open RemoteWatchHistory delivery automatically.
            send_resp = await client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "comms_send",
                        "arguments": {
                            "fork": {"name": "agy-cloud"},
                            "to": "sender",
                            "body": "got your second ping",
                            "answers": [{"seq": second.seq, "message_id": str(second.message_id)}],
                        },
                    },
                },
            )
            send_reply = await send_resp.json()
            assert not send_reply["result"]["isError"], send_reply

            after_second, = comms.views.message_notifications_for_references(
                AllOutcomes(), (second.reference,)
            ).outcomes[second.seq, second.message_id]
            assert after_second.state.is_consumed()
            assert isinstance(after_second.answer, AnsweredResponse)


def test_every_dealt_file_fits_antigravity_default_read_span() -> None:
    span = AntigravityReadFileTool.default_span()
    for path in DeckReader.sources():
        assert span.covers(path), f"{path} exceeds Antigravity single-read cap (800 lines / 46080 bytes)"


def test_antigravity_harness_installs_hooks_and_switch_toggles_them(tmp_path: Path) -> None:
    harness = AntigravityHarness(tmp_path)
    hooks_path = harness.hooks_config
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(json.dumps({"user-hook": {"PreInvocation": []}}), encoding="utf-8")

    configured = harness.configure()
    assert configured == (hooks_path,)
    saved = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert "user-hook" in saved
    gate_entry = saved[ANTIGRAVITY_HOOK_NAME]
    assert set(gate_entry) == {"SessionStart", "PreInvocation", "PreToolUse", "PostToolUse"}

    StopVerb(sender="comms-a", why="test").switched_antigravity(saved)
    assert saved[ANTIGRAVITY_HOOK_NAME]["enabled"] is False
    hooks_path.write_text(json.dumps(saved), encoding="utf-8")
    # Re-running configure preserves disabled state until explicitly started again.
    harness.configure()
    reloaded = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert reloaded[ANTIGRAVITY_HOOK_NAME]["enabled"] is False

    StartVerb().switched_antigravity(reloaded)
    assert "enabled" not in reloaded[ANTIGRAVITY_HOOK_NAME]


def test_antigravity_rule_gate_deals_on_start_and_re_deals_after_checkpoint_compaction(
    tmp_path: Path,
) -> None:
    real_sources = DeckReader.sources()
    assert len(real_sources) >= 2

    workspace = tmp_path / "repo"
    (workspace / "src/agent_comms/team/rules").mkdir(parents=True)
    transcript = tmp_path / "brain" / "conv-1" / ".system_generated/logs/transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-24T04:00:00Z",
            "content": "<USER_REQUEST>\nFix Antigravity compaction dealing.\n</USER_REQUEST>",
        }) + "\n",
        encoding="utf-8",
    )

    base = {
        "conversationId": "conv-1",
        "transcriptPath": str(transcript),
        "artifactDirectoryPath": str(tmp_path / "brain" / "conv-1"),
        "workspacePaths": [str(workspace)],
        "cwd": str(workspace),
    }

    # 1. SessionStart deals real DeckReader sources and injects the deal order.
    started = handle_hook({**base, "hookEventName": "SessionStart"})
    injected = started["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert str(real_sources[0].resolve()) in injected
    assert "Fix Antigravity compaction dealing." in injected

    # 2. PreToolUse denies ordinary run_command and unrelated reads, but allows dealt reads,
    #    emergency stop, comms_mcp.py wake-on, and comms MCP tools.
    denied = handle_hook({
        **base,
        "hookEventName": "PreToolUse",
        "toolCall": {"id": "1", "name": "run_command", "args": {"CommandLine": "pytest"}},
    })
    assert denied["decision"] == "deny"

    assert handle_hook({
        **base,
        "hookEventName": "PreToolUse",
        "toolCall": {"id": "2", "name": "call_mcp_tool", "args": {"ServerName": "comms", "ToolName": "comms_send"}},
    }) == {"decision": "allow"}

    assert handle_hook({
        **base,
        "hookEventName": "PreToolUse",
        "toolCall": {"id": "3", "name": "run_command", "args": {"CommandLine": "python3 comms_mcp.py wake-on /tmp/x.log"}},
    }) == {"decision": "allow"}

    assert handle_hook({
        **base,
        "hookEventName": "PreToolUse",
        "toolCall": {"id": "4", "name": "run_command", "args": {"CommandLine": "agent-comms-hooks stop --from comms-a --why test"}},
    }) == {"decision": "allow"}

    # Failed read_file must NOT count as read.
    first_source = real_sources[0].resolve()
    assert handle_hook({
        **base,
        "hookEventName": "PreToolUse",
        "toolCall": {"id": "r0", "name": "read_file", "args": {"Path": str(first_source)}},
    }) == {"decision": "allow"}
    handle_hook({
        **base,
        "hookEventName": "PostToolUse",
        "error": "permission denied",
        "toolCall": {"id": "r0", "name": "read_file", "args": {"Path": str(first_source)}},
    })
    assert handle_hook({
        **base,
        "hookEventName": "PreToolUse",
        "toolCall": {"id": "x", "name": "run_command", "args": {"CommandLine": "ls"}},
    })["decision"] == "deny"

    # Complete every real dealt read via PreToolUse + PostToolUse in order.
    for idx, path in enumerate(real_sources):
        resolved = path.resolve()
        assert handle_hook({
            **base,
            "hookEventName": "PreToolUse",
            "toolCall": {"id": f"pre-{idx}", "name": "read_file", "args": {"Path": str(resolved)}},
        }) == {"decision": "allow"}
        handle_hook({
            **base,
            "hookEventName": "PostToolUse",
            "toolCall": {"id": f"post-{idx}", "name": "read_file", "args": {"Path": str(resolved)}},
        })

    # Toggle audit=off on the stored deal to verify gate-open fast-path + compaction re-deal.
    deal_path = transcript.with_suffix(".deal.json")
    deal_doc = json.loads(deal_path.read_text(encoding="utf-8"))
    deal_doc["audit"] = "off"
    deal_path.write_text(json.dumps(deal_doc), encoding="utf-8")

    # Refresh cursor through PreInvocation: gate is now open and fast-path returns allow.
    assert handle_hook({**base, "hookEventName": "PreInvocation"}) == {}
    pre_tool_payload = {
        **base,
        "hookEventName": "PreToolUse",
        "toolCall": {"id": "5", "name": "run_command", "args": {"CommandLine": "ls"}},
    }
    assert agy_hook_fast_path(pre_tool_payload) == {"decision": "allow"}

    # 3. Append a CHECKPOINT record to transcript.jsonl (simulating Antigravity compaction):
    #    fast-path MUST return None (busting cache), PreInvocation MUST inject a fresh CompactedDeal
    #    with Tristan's verbatim prompt, and PreToolUse MUST deny ordinary tools again!
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "step_index": 42,
            "source": "SYSTEM",
            "type": "CHECKPOINT",
            "status": "DONE",
            "created_at": "2026-11-24T04:42:47Z",
            "content": "Summary of earlier turns",
        }) + "\n")

    assert agy_hook_fast_path(pre_tool_payload) is None
    redeal = handle_hook({**base, "hookEventName": "PreInvocation"})
    redeal_text = redeal["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "2026-11-24T04:42:47Z" in redeal_text
    assert "Fix Antigravity compaction dealing." in redeal_text
    assert handle_hook(pre_tool_payload)["decision"] == "deny"


def test_comms_mcp_cloud_agy_hook_fallback_deals_and_redeals_on_checkpoint(tmp_path: Path) -> None:
    import subprocess
    import sys

    script = Path(__file__).resolve().parents[1] / ".agents/skills/comms-cloud-connect/scripts/comms_mcp.py"
    repo = tmp_path / "agent-comms"
    (repo / "src/agent_comms/team/paper/semantic-completeness").mkdir(parents=True)
    (repo / "src/agent_comms/team/rules").mkdir(parents=True)
    paper = repo / "src/agent_comms/team/paper/semantic-completeness/01.md"
    rule = repo / "src/agent_comms/team/rules/spine.md"
    paper.write_text("# Paper\nBody.\n", encoding="utf-8")
    rule.write_text("# Spine\nBody.\n", encoding="utf-8")

    transcript = tmp_path / "conv" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-24T04:00:00Z",
            "content": "<USER_REQUEST>\nCloud compaction test.\n</USER_REQUEST>",
        }) + "\n",
        encoding="utf-8",
    )

    def invoke(payload: dict[str, object]) -> dict[str, object]:
        proc = subprocess.run(
            [sys.executable, str(script), "agy-hook", "--cloud-only", "--repo", str(repo)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(proc.stdout)

    base = {"transcriptPath": str(transcript), "cwd": str(repo), "workspacePaths": [str(repo)]}
    started = invoke({**base, "hookEventName": "SessionStart"})
    assert str(paper.resolve()) in started["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert invoke({
        **base,
        "hookEventName": "PreToolUse",
        "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest"}},
    })["decision"] == "deny"

    for dealt in (paper, rule):
        invoke({
            **base,
            "hookEventName": "PostToolUse",
            "toolCall": {"name": "read_file", "args": {"Path": str(dealt)}},
        })
    assert invoke({
        **base,
        "hookEventName": "PreToolUse",
        "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest"}},
    }) == {"decision": "allow"}

    # Compaction CHECKPOINT re-closes the cloud gate and injects verbatim user words.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "step_index": 99,
            "source": "SYSTEM",
            "type": "CHECKPOINT",
            "status": "DONE",
            "created_at": "2026-11-24T04:55:00Z",
            "content": "Compacted summary",
        }) + "\n")

    redeal = invoke({**base, "hookEventName": "PreInvocation"})
    msg = redeal["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "2026-11-24T04:55:00Z" in msg
    assert "Cloud compaction test." in msg
    assert invoke({
        **base,
        "hookEventName": "PreToolUse",
        "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest"}},
    })["decision"] == "deny"
