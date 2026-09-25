"""Real Pi: old settlement cannot terminate an identified late follow-up."""

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
@pytest.mark.parametrize("delayed_settlement", [False, True])
async def test_native_late_followup_outlives_previous_settlement(monkeypatch, delayed_settlement):
    native = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native:
        pytest.skip("Set AC_NATIVE_STACK_BIN to the prepared native launcher")
    with TemporaryDirectory(prefix="ac-native-settlement-", dir="/var/tmp") as raw:
        root = Path(raw)
        agent = root / "agent"
        agent.mkdir(mode=0o700)
        second_started = threading.Event()
        release_second = threading.Event()
        old_settled = root / "release-old-settlement"
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                index = len(requests)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    if index == 2:
                        second_started.set()
                        while not release_second.wait(0.025):
                            self.wfile.write(b": heartbeat\n\n")
                            self.wfile.flush()
                    event = {
                        "id": f"answer-{index}",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "z-ai/glm-5.3-flash",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": f"FINAL_{index}"},
                                "finish_reason": None,
                            }
                        ],
                    }
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    event["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                    event["usage"] = {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                    }
                    self.wfile.write(f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
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
        args = [
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
        ]
        if delayed_settlement:
            extension = root / "delay-settlement.ts"
            extension.write_text(
                "import {existsSync} from 'node:fs';export default function(pi){let count=0;"
                "pi.on('agent_settled',async()=>{if(++count===1){"
                f"while(!existsSync({json.dumps(str(old_settled))})) "
                "await new Promise(resolve=>setTimeout(resolve,10));}});};"
            )
            args += ["--extension", str(extension)]
        for key in (
            "PI_AGENT_ID",
            "PI_PARENT_ID",
            "PI_TASK",
            "PI_WORKTREE",
            "AGENT_COMMS_THREAD",
            "AGENT_COMMS_MANAGED",
            "AGENT_COMMS_ROOT",
        ):
            monkeypatch.delenv(key, raising=False)
        env = {
            "PI_CODING_AGENT_DIR": str(agent),
            "OPENROUTER_API_KEY": "localhost-only",
            "PI_OFFLINE": "1",
            "NODE_OPTIONS": f"--require={preload}",
            "AGENT_COMMS_ROOT": str(root / "wire"),
        }
        queue = asyncio.Queue()
        finish_event = asyncio.Event()
        persistent = backend.PersistentPiSession()
        events = []
        injected = False
        release_task = None

        async def release_response():
            await asyncio.sleep(0.5)
            release_second.set()

        try:
            async with asyncio.timeout(15):
                async for event in backend.stream_agent_events(
                    native,
                    args,
                    "ORIGINAL_INPUT",
                    str(root),
                    env_extra=env,
                    session_file=str(root / "session.jsonl"),
                    steering_queue=queue,
                    persistent_session=persistent,
                    finish_event=finish_event,
                    native_start=lambda *_: True,
                ):
                    events.append(event)
                    if event.get("type") == "settled":
                        finish_event.set()
                    if event.get("type") == "provider_usage" and not injected:
                        injected = True
                        await queue.put(
                            {
                                "type": "prompt",
                                "message": "LATE_FOLLOWUP",
                                "streamingBehavior": "steer",
                                "_input_id": "late",
                            }
                        )
                        async with asyncio.timeout(5):
                            while not second_started.is_set():
                                await asyncio.sleep(0.01)
                        old_settled.touch()
                        release_task = asyncio.create_task(release_response())
            final = events[-1]
            starts = [event.get("id") for event in events if event.get("type") == "input_started"]
            assert len(requests) == 2 and starts == [None, "late"], (len(requests), starts, events)
            assert final.get("ok") is True, events
            settled_indices = [
                i for i, event in enumerate(events) if event.get("type") == "settled"
            ]
            final_chunk = next(
                i
                for i, event in enumerate(events)
                if event.get("type") == "chunk" and "FINAL_2" in event.get("text", "")
            )
            assert len(settled_indices) == 1 and settled_indices[0] > final_chunk, events
            assert json.dumps(requests[1]).count("LATE_FOLLOWUP") == 1
            assert "FINAL_2" in final.get("text", ""), events
        finally:
            release_second.set()
            old_settled.touch()
            if release_task is not None:
                await release_task
            await persistent.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
