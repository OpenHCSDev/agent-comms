"""Real native Pi must not feed the full UNKNOWN backlog back to its provider."""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms import backend, wire
from agent_comms.acp import CommsAgent
from agent_comms.input_disposition import InputDispositions


async def test_native_repeated_inbox_keeps_unknown_backlog_out_of_context(monkeypatch):
    native = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native:
        pytest.skip("Requires the prepared native Pi stack")
    with TemporaryDirectory(prefix="ac-native-inbox-", dir="/var/tmp") as directory:
        root = Path(directory)
        requests = []
        events = []
        failures = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    requests.append(request)
                    index = len(requests)
                    if index in {2, 3}:
                        for message in request["messages"]:
                            if message.get("role") == "tool":
                                size = len(message["content"].encode())
                                assert size < 32 * 1024, (
                                    "Full UNKNOWN backlog reached provider "
                                    f"request {index}: {size} bytes"
                                )
                    if index > 4:
                        failures.append("Inbox inspection made an unexpected provider call")
                        self.send_error(400, "Unexpected provider call")
                        return
                    if index <= 2:
                        delta = {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"inspect_inbox_{index}",
                                    "type": "function",
                                    "function": {
                                        "name": "comms_inbox",
                                        "arguments": json.dumps({"thread": "parent", "ack": True}),
                                    },
                                }
                            ],
                        }
                        finish = "tool_calls"
                    elif index == 3:
                        tool_messages = [
                            message
                            for message in request["messages"]
                            if message.get("role") == "tool"
                        ]
                        latest = json.loads(tool_messages[-1]["content"])
                        assert latest["complete"] is False
                        delta = {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "read_inbox_excerpt",
                                    "type": "function",
                                    "function": {
                                        "name": "read",
                                        "arguments": json.dumps(
                                            {
                                                "path": latest["result_file"],
                                                "offset": 5,
                                                "limit": 10,
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                        finish = "tool_calls"
                    else:
                        delta = {"content": "INBOX_INSPECTED_TWICE"}
                        finish = "stop"
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
                    self.send_error(400, "Invalid fixture request")

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
        (config / "settings.json").write_text(json.dumps({"compaction": {"enabled": True}}))
        preload = root / "local-only.cjs"
        preload.write_text(
            "const original=globalThis.fetch;globalThis.fetch=(url,...rest)=>{"
            "const link=url instanceof Request?url.url:String(url);"
            f"if(!link.startsWith('http://127.0.0.1:{server.server_port}/')) "
            "throw new Error('BLOCKED_NONLOCAL_NETWORK');return original(url,...rest);};"
        )
        for key in (
            "PI_AGENT_ID",
            "PI_PARENT_ID",
            "PI_TASK",
            "PI_WORKTREE",
            "AGENT_COMMS_THREAD",
            "AGENT_COMMS_MANAGED",
        ):
            monkeypatch.delenv(key, raising=False)
        source = Path(__file__).resolve().parents[1]
        for key, value in {
            "PI_CODING_AGENT_DIR": str(config),
            "OPENROUTER_API_KEY": "local-only",
            "NODE_OPTIONS": f"--require={preload}",
            "AGENT_COMMS_ROOT": str(root / "wire"),
            "AGENT_COMMS_AGENT_MODELS": "openrouter/z-ai/glm-5.3-flash",
            "PYTHONPATH": str(Path(backend.__file__).resolve().parents[1]),
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
            str(source / "extensions/pi-agent-comms/index.ts"),
        ]
        stream = backend.stream_agent_events

        async def collect_events(*args, **kwargs):
            async for event in stream(*args, **kwargs):
                events.append(event)
                yield event

        monkeypatch.setattr(backend, "stream_agent_events", collect_events)
        comms = wire(root / "wire")
        agent = CommsAgent(comms, agent_bin=native, agent_args=args, runtime_enabled=True)
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
        try:
            project = root / "parent"
            project.mkdir()
            await agent.new_session(str(project))
            admission = comms.registry.snapshot().admission_generations["parent"]
            old_rows = {
                f"acp:old-{index:04d}": {
                    "key": f"acp:old-{index:04d}",
                    "sequence": None,
                    "owner": "parent",
                    "admission": admission,
                    "target": "parent",
                    "source_text": f"OLD_UNKNOWN_BODY_{index:04d}:" + "x" * 880,
                    "turn_id": None,
                    "native_id": None,
                    "sent_text": None,
                    "status": "unknown",
                }
                for index in range(935)
            }
            # Populate the durable ledger once; setup must not perform 935 rewrites.
            agent._dispositions._write(old_rows)
            full_result = {
                "messages": [],
                "acknowledged": 0,
                "unresolved_inputs": [InputDispositions.public(row) for row in old_rows.values()],
            }
            assert 900_000 < len(json.dumps(full_result, indent=2).encode()) < 1_100_000
            await asyncio.wait_for(
                agent._run_owned_input("parent", "parent", "Inspect the inbox twice, then finish."),
                45,
            )
            assert not failures, failures
            assert len(requests) == 4
            assert any(t["function"]["name"] == "comms_inbox" for t in requests[0]["tools"])
            artifacts = []
            for index, request in enumerate(requests[1:], start=1):
                tools = [
                    message for message in request["messages"] if message.get("role") == "tool"
                ]
                assert len(tools) == index
                for message in tools[:2]:
                    text = message["content"]
                    assert len(text.encode()) < 32 * 1024, "Full UNKNOWN backlog reached provider"
                    assert "OLD_UNKNOWN_BODY_" not in text
                    result = json.loads(text)
                    assert result["complete"] is False
                    assert result["ackDeferred"] is True
                    assert result["counts"] == {"messages": 0, "unresolved_inputs": 935}
                    assert result["messages"] == result["unresolved_inputs"] == []
                    artifact = Path(result["result_file"])
                    assert artifact.is_absolute()
                    assert json.loads(artifact.read_text()) == full_result
                    artifacts.append(artifact)
            excerpt = requests[-1]["messages"][-1]["content"]
            assert len(excerpt.encode()) < 32 * 1024
            assert "OLD_UNKNOWN_BODY_0000:" in excerpt
            assert "OLD_UNKNOWN_BODY_0001:" not in excerpt
            assert len(set(artifacts)) == 1
            assert not any(event["type"].startswith("compaction") for event in events)
            assert not any(event["type"] == "error" for event in events)
            terminal = [event for event in events if event["type"] == "done"]
            assert len(terminal) == 1 and terminal[0]["ok"] is True
            assert terminal[0]["text"] == "INBOX_INSPECTED_TWICE"
            assert len([event for event in events if event["type"] == "input_started"]) == 1
            saved_rows = agent._dispositions._read()
            assert {key: saved_rows[key] for key in old_rows} == old_rows
            transcript = Path(comms.registry.require("parent").session_file)
            rows = [json.loads(line) for line in transcript.read_text().splitlines()]
            assert not any(row.get("type") == "compaction" for row in rows)
            messages = [row["message"] for row in rows if "message" in row]
            users = [message for message in messages if message.get("role") == "user"]
            assert len(users) == 1 and "OLD_UNKNOWN_BODY_" not in json.dumps(users)
            results = [message for message in messages if message.get("toolName") == "comms_inbox"]
            assert len(results) == 2 and not any(message.get("isError") for message in results)
            reads = [message for message in messages if message.get("toolName") == "read"]
            assert len(reads) == 1 and not reads[0].get("isError")
        finally:
            await agent.shutdown()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)
