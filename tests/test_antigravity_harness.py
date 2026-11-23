"""Antigravity harness installation, transcript decoding, and live inbox-watch streaming."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer, unused_port
from yarl import URL

from agent_comms.cli_bridges.antigravity import AntigravityBridge
from agent_comms.cli_bridges.antigravity_transcript import (
    AntigravityTranscript,
    RunningAntigravity,
)
from agent_comms.cli_bridges.history import OfflineHistory, OnlineHistory
from agent_comms.cli_bridges.inbox_watch_client import (
    AntigravityFollowedTranscript,
    FollowedTranscript,
    InboxSocket,
    session_transcript,
)
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
from agent_comms.transport.connector_store import ConnectorStore


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
