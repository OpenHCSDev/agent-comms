"""Real Pi RPC publishes committed channel progress before the final turn."""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms import Thread, wire
from agent_comms.acp import CommsAgent
from agent_comms.declarations import Message, MessageType


async def test_native_tool_use_progress_reaches_channel_before_final(monkeypatch):
    native = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native:
        pytest.skip("Requires the prepared native Pi stack")
    with TemporaryDirectory(prefix="ac-native-routed-progress-", dir="/var/tmp") as directory:
        root = Path(directory)
        comms = wire(root / "wire")
        requests: list[dict] = []
        observed: list[tuple[str, bool]] = []
        failures: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    requests.append(request)
                    index = len(requests)
                    if index == 2:
                        observed.extend(
                            (message.body, message.notice)
                            for message in comms.channel_history("#team")
                        )
                    assert index <= 2
                    if index == 1:
                        delta = {
                            "role": "assistant",
                            "content": "Working",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "read_only_tool",
                                    "type": "function",
                                    "function": {
                                        "name": "bash",
                                        "arguments": json.dumps({"command": "true"}),
                                    },
                                }
                            ],
                        }
                        finish = "tool_calls"
                    else:
                        delta = {"role": "assistant", "content": "Done"}
                        finish = "stop"
                    chunk = {
                        "id": f"local-{index}",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "z-ai/glm-5.3-flash",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 10,
                            "total_tokens": 110,
                        },
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
                    self.wfile.flush()
                except Exception as error:
                    failures.append(repr(error))

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        config = root / "agent"
        config.mkdir(mode=0o700)
        (config / "auth.json").write_text("{}")
        (config / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "openrouter": {"baseUrl": f"http://127.0.0.1:{server.server_port}/v1"}
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
        for key, value in {
            "PI_CODING_AGENT_DIR": str(config),
            "OPENROUTER_API_KEY": "local-only",
            "NODE_OPTIONS": f"--require={preload}",
            "AGENT_COMMS_ROOT": str(root / "wire"),
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        }.items():
            monkeypatch.setenv(key, value)
        args = [
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
            "--provider",
            "openrouter",
            "--model",
            "z-ai/glm-5.3-flash",
            "--thinking",
            "off",
            "--extension",
            str(Path(__file__).resolve().parents[1] / "extensions/pi-agent-comms/index.ts"),
        ]
        agent = CommsAgent(comms, agent_bin=native, agent_args=args, runtime_enabled=True)
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
        try:
            project = root / "worker"
            project.mkdir()
            await agent.new_session(str(project))
            comms.register(Thread("member", frozenset({"team"}), str(project)))
            human = comms.user_identity(str(project))
            origin = Message(human.name, "#team", "Please help", MessageType.INFO)
            await asyncio.wait_for(
                agent._run_agent_turn(
                    "worker",
                    "worker",
                    "Please help",
                    origins=(origin,),
                    reply_targets=("#team",),
                ),
                40,
            )
            assert not failures, failures
            assert len(requests) == 2
            assert observed == [("Working", True)]
            assert [
                (message.body, message.notice) for message in comms.channel_history("#team")
            ] == [("Working", True), ("Done", False)]
        finally:
            await agent.shutdown()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)
