"""Actual native Pi receipts for queued and steered channel requests."""

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms import Thread, wire
from agent_comms.acp import CommsAgent


@pytest.mark.parametrize("case", ["deliver", "goal", "stop", "reopen", "steer"])
async def test_native_channel_input_receipt_and_revocation(case, monkeypatch):
    native = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native:
        pytest.skip("Requires prepared native Pi stack")
    with TemporaryDirectory(prefix="ac-native-channel-", dir="/var/tmp") as directory:
        root = Path(directory)
        requests = []
        release = threading.Event()
        started = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                started.set()
                assert release.wait(15), "Test did not release localhost response"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunk = {
                    "id": f"response-{len(requests)}",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "z-ai/glm-5.3-flash",
                    "choices": [
                        {"index": 0, "delta": {"content": "RECEIVED"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                }
                self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
                self.wfile.flush()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        config = root / "agent"
        config.mkdir(mode=0o700)
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
            "AGENT_COMMS_AGENT_MODELS": "openrouter/z-ai/glm-5.3-flash",
        }.items():
            monkeypatch.setenv(key, value)
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
        comms = wire(root / "wire")
        agent = CommsAgent(comms, agent_bin=native, agent_args=args, runtime_enabled=True)
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
        monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
        project = root / "worker"
        project.mkdir()
        turn = None
        try:
            await agent.new_session(str(project))
            comms.update_tags("worker", add=frozenset({"team"}))
            comms.register(Thread("peer", frozenset({"team"}), str(project)))
            turn = asyncio.create_task(agent._run_owned_input("worker", "worker", "Warmup"))
            assert await asyncio.to_thread(started.wait, 15)
            if case != "steer":
                release.set()
                await asyncio.wait_for(turn, 20)
                message = comms.send_user_message(
                    "#team", "QUEUED_CHANNEL_REQUEST", worktree=str(project)
                )
            else:
                message = comms.send_message("peer", "#team", "@worker STEER_CHANNEL_REQUEST")
            await agent._drain_inbox("worker")
            key = agent._dispositions.bus_key(message, comms.registry.require("worker"))
            assert agent._dispositions.status(key) == "unknown"
            if case == "goal":
                comms.update_goal("worker", "set", text="Changed goal")
            elif case == "stop":
                comms.stop("worker")
            elif case == "reopen":
                await agent.shutdown()
                agent = CommsAgent(
                    wire(comms.root), agent_bin=native, agent_args=args, runtime_enabled=True
                )
                monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
                await agent.load_session(str(project), "worker")
                await agent._drain_inbox("worker")
            if case == "steer":
                release.set()
                await asyncio.wait_for(turn, 20)
            elif case != "reopen":
                CommsAgent._schedule_wake(agent, "worker")
                await asyncio.wait_for(agent._wake_tasks["worker"], 20)
            success = case in {"deliver", "steer"}
            assert len(requests) == (2 if success else 1)
            assert agent._dispositions.status(key) == ("started" if success else "unknown")
            assert not agent._pending_turns.get("worker")
            rows = [
                json.loads(line)
                for line in Path(comms.registry.require("worker").session_file)
                .read_text()
                .splitlines()
            ]
            native_inputs = [
                row["message"] for row in rows if row.get("message", {}).get("role") == "user"
            ]
            matched = [row for row in native_inputs if "CHANNEL_REQUEST" in json.dumps(row)]
            assert len(matched) == int(success)
            if success:
                assert matched[0]["inputId"] == agent._dispositions.get(key)["native_id"]
            if case == "deliver":
                assert comms.channel_history("#team")[-1].body == "RECEIVED"
            if not success:
                updates = []

                class Client:
                    async def session_update(self, **kwargs):
                        updates.append(kwargs["update"].model_dump(by_alias=True))

                await agent.replay_unknown_inputs("worker", Client())
                assert any(
                    row["_meta"]["agentComms"]["inputDisposition"]["sequence"] == message.seq
                    for row in updates
                )
        finally:
            release.set()
            if turn is not None and not turn.done():
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
            await agent.shutdown()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)
