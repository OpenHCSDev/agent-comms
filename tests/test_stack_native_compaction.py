"""Opt-in integration of the pinned Pi RPC, backend, and localhost provider."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from agent_comms import backend

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native Pi uses POSIX fsync")


def _saved_history(path: Path, cwd: Path, *, short: bool = False) -> None:
    """Create a real Pi branch whose last usage exceeds a 128K model window."""
    timestamp = "2026-09-24T00:00:00.000Z"
    rows: list[dict] = [
        {
            "type": "session", "version": 3, "id": str(uuid4()),
            "timestamp": timestamp, "cwd": str(cwd), "parentSession": None,
        }
    ]
    parent = None
    for index in range(100):
        user_id, assistant_id = f"{2 * index + 1:08x}", f"{2 * index + 2:08x}"
        rows.append({
            "type": "message", "id": user_id, "parentId": parent, "timestamp": timestamp,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "x" * (1000 if short else 6000)}],
                "timestamp": 1790290000000 + 2 * index,
            },
        })
        rows.append({
            "type": "message", "id": assistant_id, "parentId": user_id,
            "timestamp": timestamp,
            "message": {
                "role": "assistant", "content": [{"type": "text", "text": "ack"}],
                "api": "openai-completions", "provider": "openrouter",
                "model": "z-ai/glm-5.3-flash", "stopReason": "stop",
                "rawStopReason": "stop", "timestamp": 1790290000001 + 2 * index,
                "usage": {
                    "input": 200000, "output": 1, "cacheRead": 0,
                    "cacheWrite": 0, "reasoning": 0, "totalTokens": 200001,
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
                },
            },
        })
        parent = assistant_id
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    path.chmod(0o600)


@pytest.mark.parametrize(
    "case", ["success", "summary_failure", "oversized_current", "oversized_summary"]
)
async def test_saved_history_compacts_after_native_user_start(case: str) -> None:
    native_bin = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native_bin:
        pytest.skip("Set AC_NATIVE_STACK_BIN to the prepared pinned Pi launcher")
    with TemporaryDirectory(prefix="ac-native-compaction-", dir="/var/tmp") as raw:
        root = Path(raw)
        project = root / "project"
        (project / ".pi").mkdir(parents=True, mode=0o700)
        (project / ".pi" / "settings.json").write_text(json.dumps({
            "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}},
            "compaction": {"enabled": True},
        }))
        session = root / "saved.jsonl"
        _saved_history(
            session, project, short=case in {"oversized_current", "oversized_summary"}
        )
        agent = root / "agent"
        agent.mkdir(mode=0o700)
        calls: list[str] = []
        reasoning_efforts: list[str | None] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                calls.append(self.path)
                reasoning_efforts.append(request.get("reasoning", {}).get("effort"))
                if case == "summary_failure":
                    body = b'{"error":{"message":"429 rate limit","type":"rate_limit_error"}}'
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                chunk = {
                    "id": f"fixture-{len(calls)}", "object": "chat.completion.chunk",
                    "created": 12345, "model": "z-ai/glm-5.3-flash",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": (
                        "y" * 600000 if case == "oversized_summary" else "summary"
                    )},
                                 "finish_reason": None}],
                }
                terminal = {
                    **chunk,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 3, "total_tokens": 103},
                }
                body = (
                    "".join(f"data: {json.dumps(row)}\n\n" for row in (chunk, terminal))
                    + "data: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            (agent / "models.json").write_text(json.dumps({
                "providers": {"openrouter": {
                    "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                    "modelOverrides": {"z-ai/glm-5.3-flash": {
                        "contextWindow": 128000, "maxTokens": 4096,
                    }},
                }},
            }))
            (agent / "models.json").chmod(0o600)
            preload = root / "local-only.cjs"
            preload.write_text(
                "const original=globalThis.fetch;"
                "globalThis.fetch=(url,...rest)=>{"
                "const link=url instanceof Request?url.url:String(url);"
                f"if(!link.startsWith('http://127.0.0.1:{server.server_port}/')) "
                "throw new Error('BLOCKED_NONLOCAL_NETWORK');"
                "return original(url,...rest);};"
            )
            events = []
            async def collect():
                async for event in backend.stream_agent_events(
                    native_bin,
                    ["--offline", "--no-extensions", "--no-skills", "--no-prompt-templates",
                     "--no-context-files", "--no-tools", "--provider", "openrouter",
                     "--model", "z-ai/glm-5.3-flash", "--thinking", "high"],
                    "x" * 600000 if case == "oversized_current" else "Reply OK.", str(project),
                    env_extra={
                        "PI_CODING_AGENT_DIR": str(agent),
                        "OPENROUTER_API_KEY": "offline-fixture-no-real-key",
                        "NODE_OPTIONS": f"--require={preload}", "PI_OFFLINE": "1",
                    },
                    session_file=str(session), require_input_id=True,
                    native_start=lambda *_: True,
                ):
                    events.append(event)
            await asyncio.wait_for(collect(), timeout=30)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        kinds = [event["type"] for event in events]
        assert "input_started" in kinds
        assert events[-1]["type"] == "done"
        if case == "summary_failure":
            assert events[-1]["ok"] is False
            assert "compaction" in str(events[-1]["text"]).lower()
            assert len(calls) == 1  # No automatic replay of a refused attempt.
            return
        if case == "oversized_current":
            assert events[-1]["ok"] is False
            assert "oversized" in str(events[-1]["text"]).lower()
            assert calls == []  # A single uncompactable user input costs no summary call.
            return
        if case == "oversized_summary":
            assert events[-1]["ok"] is False
            assert "oversized" in str(events[-1]["text"]).lower()
            assert len(calls) == 1  # Committed summary, then refusal before the user prompt.
            return
        assert kinds.index("input_started") < kinds.index("compaction_start")
        assert kinds.index("compaction_start") < kinds.index("compaction_end")
        assert events[-1]["ok"] is True
        assert len(calls) > 1
        assert reasoning_efforts[:-1] == ["low"] * (len(calls) - 1)
        assert reasoning_efforts[-1] == "high"
        assert len(
            [event for event in events if event["type"] == "compaction_progress"]
        ) == len(calls) - 1
        assert len([event for event in events if event["type"] == "provider_usage"]) == len(calls)
