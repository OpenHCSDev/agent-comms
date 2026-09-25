"""Real native Pi goal tool, localhost provider, and dependency-triggered owner turn."""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms import GoalExecutionState, Thread, wire
from agent_comms.acp import CommsAgent


@pytest.mark.parametrize("restart", [False, True])
async def test_native_goal_standby_then_exact_child_input(monkeypatch, restart):
    native = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native:
        pytest.skip("Requires the prepared native Pi stack")
    with TemporaryDirectory(prefix="ac-native-standby-", dir="/var/tmp") as directory:
        root = Path(directory)
        requests = []
        failures = []
        goal_id = None

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    requests.append(request)
                    index = len(requests)
                    assert index <= 4, "Standby must not schedule another provider turn"
                    delta = {
                        "content": "Waiting for child." if index == 2 else "Reviewed and done."
                    }
                    finish = "stop"
                    if index in {1, 3}:
                        schema = next(
                            t["function"]
                            for t in request["tools"]
                            if t["function"]["name"] == "comms_goal"
                        )
                        assert "standby" in schema["parameters"]["properties"]["status"]["enum"]
                        arguments = {
                            "goal_id": goal_id,
                            "status": "standby" if index == 1 else "completed",
                            "progress": (
                                "Waiting for child" if index == 1 else "Verified child report"
                            ),
                        }
                        if index == 1:
                            arguments["wait_for"] = ["@child"]
                        delta = {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"goal_{index}",
                                    "type": "function",
                                    "function": {
                                        "name": "comms_goal",
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        }
                        finish = "tool_calls"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunk = {
                        "id": f"response-{index}",
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
        auth_file = config / "auth.json"
        auth_file.write_text("{}")
        auth_file.chmod(0o600)
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
        comms = wire(root / "wire")
        agent = CommsAgent(comms, agent_bin=native, agent_args=args, runtime_enabled=True)
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
        try:
            project = root / "parent"
            project.mkdir()
            await agent.new_session(str(project))
            comms.register(Thread("child", frozenset(), str(project)))
            goal = comms.update_goal(
                "parent", "set", text="Review @child report", owner_store=agent._open_goal_store()
            )
            goal_id = goal.id
            agent._schedule_goal("parent")
            await asyncio.wait_for(agent._wake_tasks["parent"], 40)
            assert not failures, failures
            assert comms.registry.require("parent").goal.active
            assert comms.goal_execution("parent").state is GoalExecutionState.STANDBY
            assert len(requests) == 2
            first_proc = agent._persistent_backends["parent"].proc
            assert first_proc is not None and first_proc.returncode is None
            agent._schedule_goal("parent")
            assert not agent._pending_turns.get("parent")
            assert agent._goal_store.snapshot(goal.id).number == 2
            if restart:
                # Wait intent survives reopening. The new executing owner may
                # rotate only the unused READY grant at the send boundary.
                await agent.shutdown()
                comms = wire(root / "wire")
                agent = CommsAgent(comms, agent_bin=native, agent_args=args, runtime_enabled=True)
                monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
                await agent.load_session(str(project), "parent")
                agent._schedule_goal("parent")
                assert comms.goal_execution("parent").state is GoalExecutionState.STANDBY
                assert not agent._pending_turns.get("parent") and len(requests) == 2
            message = comms.send_message("child", "parent", "CHILD_REPORT_EXACT_NATIVE_INPUT")
            await agent._drain_inbox("parent")
            await asyncio.wait_for(agent._wake_tasks["parent"], 40)
            assert not failures, failures
            assert len(requests) == 4
            if not restart:
                assert agent._persistent_backends["parent"].proc is first_proc
            else:
                assert first_proc.returncode is not None
            assert agent._dispositions.status(f"bus:{message.seq}") == "started"
            assert comms.registry.require("parent").goal.status == "completed"
            assert agent._goal_store.snapshot(goal.id).state == "completed"
            session = Path(comms.registry.require("parent").session_file)
            rows = [json.loads(line) for line in session.read_text().splitlines()]
            users = [row["message"] for row in rows if row.get("message", {}).get("role") == "user"]
            assert len(users) == 2 and users[1]["inputId"]
            assert message.body in json.dumps(users[1]["content"])
            tool_results = [
                row["message"]
                for row in rows
                if row.get("message", {}).get("toolName") == "comms_goal"
            ]
            assert len(tool_results) == 2 and not any(row.get("isError") for row in tool_results)
        finally:
            await agent.shutdown()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)
