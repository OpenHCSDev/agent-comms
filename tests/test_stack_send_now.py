"""Native Pi + streaming localhost provider: Send now interrupts, never replays."""

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms import backend


@pytest.mark.asyncio
async def test_send_now_interrupts_native_response():
    native_bin = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native_bin:
        pytest.skip("Set AC_NATIVE_STACK_BIN to a prepared pinned native launcher")
    with TemporaryDirectory(prefix="ac-send-now-", dir="/var/tmp") as raw:
        root = Path(raw)
        agent = root / "agent"
        agent.mkdir(mode=0o700)
        release = threading.Event()
        cancelled = threading.Event()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                index = len(requests)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def emit(text, finish=None):
                    row = {
                        "id": f"response-{index}",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "z-ai/glm-5.3-flash",
                        "choices": [
                            {"index": 0, "delta": {"content": text}, "finish_reason": finish}
                        ],
                    }
                    self.wfile.write(f"data: {json.dumps(row)}\n\n".encode())
                    self.wfile.flush()

                try:
                    emit("OLD_PARTIAL" if index == 1 else "NEW_FINAL")
                    if index == 1:
                        while not release.wait(0.05):
                            self.wfile.write(b": heartbeat\n\n")
                            self.wfile.flush()
                    emit("", "stop")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    cancelled.set()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        (agent / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "openrouter": {
                            "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                        }
                    }
                }
            )
        )
        preload = root / "local-only.cjs"
        preload.write_text(
            "const original=globalThis.fetch;globalThis.fetch=(url,...rest)=>{"
            "const link=url instanceof Request?url.url:String(url);"
            f"if(!link.startsWith('http://127.0.0.1:{server.server_port}/')) "
            "throw new Error('BLOCKED_NONLOCAL_NETWORK');return original(url,...rest);};"
        )
        queue = asyncio.Queue()
        events = []
        first_chunk = asyncio.Event()
        started = asyncio.Event()

        async def collect():
            async for event in backend.stream_agent_events(
                native_bin,
                [
                    "--offline",
                    "--no-extensions",
                    "--no-skills",
                    "--no-context-files",
                    "--no-prompt-templates",
                    "--no-tools",
                    "--provider",
                    "openrouter",
                    "--model",
                    "z-ai/glm-5.3-flash",
                    "--thinking",
                    "off",
                ],
                "ORIGINAL_INPUT",
                str(root),
                env_extra={
                    "PI_CODING_AGENT_DIR": str(agent),
                    "OPENROUTER_API_KEY": "local-only",
                    "PI_OFFLINE": "1",
                    "NODE_OPTIONS": f"--require={preload}",
                },
                session_file=str(root / "session.jsonl"),
                steering_queue=queue,
                require_input_id=True,
                native_start=lambda *_: True,
            ):
                events.append(event)
                if event.get("type") == "chunk" and "OLD_PARTIAL" in event.get("text", ""):
                    first_chunk.set()
                if event.get("type") == "input_started" and event.get("id") == "urgent":
                    started.set()

        task = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(first_chunk.wait(), 15)
            await queue.put(
                {
                    "type": "prompt",
                    "message": "URGENT_INPUT",
                    "streamingBehavior": "steer",
                    "_input_id": "urgent",
                }
            )
            await asyncio.sleep(0.2)
            assert len(requests) == 1 and not started.is_set(), "Queue waits for a boundary"
            await queue.put({"type": "interrupt_steering", "_input_ids": ["urgent"]})
            await asyncio.wait_for(started.wait(), 3)
            await asyncio.wait_for(task, 10)
            assert not release.is_set(), "Send now must work while original response is unfinished"
            assert await asyncio.to_thread(cancelled.wait, 2), "Original request was not cancelled"
            assert len(requests) == 2, "No retry or duplicate prompt after explicit interruption"
            assert (
                sum(e.get("type") == "input_started" and e.get("id") == "urgent" for e in events)
                == 1
            )
            assert events[-1]["ok"] is True, events
            assert events[-1]["text"] == "NEW_FINAL", events
            assert not any(e.get("type") == "error" for e in events), events
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
