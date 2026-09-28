"""Cold selected-session decisions through the actual pinned Pi CLI/RPC."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

from agent_comms.native_package import verify_native_package
from agent_comms.pi_commands import AgentCommsCompactionSettings
from agent_comms.pi_events import Response
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.pi_summary_payloads import SelectedModel


@pytest.mark.parametrize(
    ("oversized", "compacted", "usage", "enabled", "trigger"),
    [
        (True, False, 0, True, True),
        (True, True, 0, True, True),
        (True, False, 0, False, True),
        (False, False, 31000, True, True),
        (False, False, 31000, False, False),
        (False, False, 100, True, False),
        (False, True, 31000, True, False),
    ],
)
async def test_cold_selected_session_decides_without_client_usage(
    tmp_path, oversized, compacted, usage, enabled, trigger
):
    package_path = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
    if not package_path:
        pytest.skip("Set PI_COMPACTION_TEST_PACKAGE to the new immutable native build")
    package = Path(package_path).resolve(strict=True)
    verify_native_package(package)
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            self.send_error(500, "Provider calls prohibited in decision test")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    config = tmp_path / "agent"
    config.mkdir()
    (config / "settings.json").write_text(
        json.dumps(
            {
                "compaction": {
                    "enabled": enabled,
                    "reserveTokens": 2048,
                    "keepRecentTokens": 1024,
                }
            }
        )
    )
    (config / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "fixture": {
                        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                        "api": "openai-completions",
                        "apiKey": "local-test",
                        "models": [
                            {
                                "id": "fixture",
                                "name": "Cold selected session",
                                "contextWindow": 32768,
                                "maxTokens": 2048,
                            }
                        ],
                    }
                }
            }
        )
    )
    session = tmp_path / "saved.jsonl"
    session_id = str(uuid4())
    timestamp = "2026-09-28T00:00:00.000Z"
    rows = [dict(type="session", version=3, id=session_id, timestamp=timestamp, cwd=str(tmp_path))]
    parent = None
    for index in range(10):
        user_id, assistant_id = f"u{index}", f"a{index}"
        for entry_id, message in (
            (
                user_id,
                dict(role="user", content="x" * (9000 if oversized else 16), timestamp=index),
            ),
            (
                assistant_id,
                dict(
                    role="assistant",
                    content=[dict(type="text", text="ack")],
                    api="openai-completions",
                    provider="fixture",
                    model="fixture",
                    stopReason="stop",
                    timestamp=index,
                    usage=dict(
                        input=usage,
                        output=0,
                        cacheRead=0,
                        cacheWrite=0,
                        totalTokens=usage,
                        cost=dict(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
                    ),
                ),
            ),
        ):
            rows.append(
                dict(type="message", id=entry_id, parentId=parent, timestamp=timestamp, message=message)
            )
            parent = entry_id
    if compacted:
        rows.append(
            dict(
                type="compaction",
                id="compacted",
                parentId=parent,
                timestamp=timestamp,
                summary="Earlier work completed.",
                firstKeptEntryId="u0" if oversized else "u9",
                tokensBefore=31000,
            )
        )
    session.write_text("".join(json.dumps(row) + "\n" for row in rows))
    session.chmod(0o600)
    original = session.read_bytes()
    env = {
        "PATH": "/usr/local/bin:/usr/bin",
        "HOME": str(tmp_path),
        "PI_CODING_AGENT_DIR": str(config),
        "AGENT_COMMS_NATIVE_CONFIG_DIR": str(config),
        "PI_OFFLINE": "1",
        "NODE_DISABLE_COMPILE_CACHE": "1",
        "NO_COLOR": "1",
    }
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "node",
            "--no-global-search-paths",
            "--import",
            str(package / "dist/agent-comms-import-fence.mjs"),
            "--import",
            str(package / "dist/agent-comms-project-bootstrap.mjs"),
            str(package / "dist/cli.js"),
            "--mode", "rpc", "--provider", "fixture", "--model", "fixture",
            "--offline", "--no-extensions", "--no-skills", "--no-prompt-templates",
            "--no-context-files", "--no-tools", "--session", str(session),
            cwd=tmp_path,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        errors = asyncio.create_task(process.stderr.read())

        async def exchange(command):
            process.stdin.write((json.dumps(command) + "\n").encode())
            await process.stdin.drain()
            async with asyncio.timeout(15):
                while True:
                    raw = await process.stdout.readline()
                    assert raw, (await errors).decode()
                    event = json.loads(raw)
                    assert event.get("type") not in ("input_committed", "context_committed")
                    if event.get("type") == "response" and event.get("id") == command["id"]:
                        return raw, event

        _, state = await exchange(dict(type="get_state", id="state"))
        assert state["success"] and state["data"]["sessionId"] == session_id
        assert state["data"]["sessionFile"] == str(session)
        request = AgentCommsCompactionSettings(
            id="decision",
            session_id=session_id,
            session_file=str(session),
            selected=SelectedModel("fixture", "fixture", 32768),
        )
        assert "contextTokens" not in request.to_rpc()
        raw, response = await exchange(request.to_rpc())
        assert response["success"], response
        decoded = PiRpcChannel.decode_record(raw, strict=True)
        assert isinstance(decoded, Response)
        assert decoded.data.decision.trigger is trigger
        assert decoded.data.decision.enabled is enabled
        assert decoded.data.decision.reserve_tokens == 2048
        assert decoded.data.selected == request.selected
        assert "contextTokens" not in response["data"]

        _, stats = await exchange(dict(type="get_session_stats", id="stats"))
        assert stats["success"], stats
        if compacted:
            assert stats["data"]["contextUsage"]["tokens"] is None
        for change in (
            dict(sessionId="changed"),
            dict(sessionFile=str(tmp_path / "other.jsonl")),
            dict(selected=replace(request.selected, provider="changed").to_wire()),
            dict(selected=replace(request.selected, model_id="changed").to_wire()),
            dict(selected=replace(request.selected, context_window=32769).to_wire()),
            dict(contextTokens=0),
        ):
            _, refusal = await exchange({**request.to_rpc(), **change, "id": "fence"})
            assert refusal["success"] is False, refusal
        assert session.read_bytes() == original
        assert calls == []
    finally:
        if process is not None:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 5)
            await errors
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
