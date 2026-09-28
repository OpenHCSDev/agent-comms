"""SelectedExecution with pinned native Pi, local deterministic coding provider."""

import asyncio
import json
import os
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordination_store import IdentityConflict, MutationStore
from test_coordinated_runtime import _root, tmp_path


async def test_native_full_four_tools_publish_and_release(tmp_path, monkeypatch):
    package = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not package:
        pytest.skip("Prepared native package required; never build or call a paid provider")
    root, root_id, comms, initial, people = _root(tmp_path, direct=True, claims=True)
    owner = comms.registry.require("beta")
    comms.registry.register(replace(owner, model="selected-offline/fixture", thinking_level="low"))
    (tmp_path / "input.txt").write_text("state=BEFORE\n")
    requests = []
    failures = []
    calls = [
        ("read", {"path": "input.txt"}),
        ("edit", {"path": "input.txt", "edits": [{"oldText": "BEFORE", "newText": "AFTER"}]}),
        ("write", {"path": "nested/result.txt", "content": "state=AFTER\n"}),
        (
            "bash",
            {
                "command": "python -c \"from pathlib import Path; assert Path('input.txt').read_text() == Path('nested/result.txt').read_text() == 'state=AFTER\\n'\""
            },
        ),
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                assert request["model"] == "fixture"
                assert self.headers["Authorization"] == "Bearer offline-only-fixture"
                assert len(requests) <= 2
                if len(requests) == 1:
                    assert {t["function"]["name"] for t in request["tools"]} == {
                        n for n, _ in calls
                    }
                    delta = {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": i,
                                "id": f"coding_{i}",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                            for i, (name, arguments) in enumerate(calls)
                        ],
                    }
                    reason = "tool_calls"
                else:
                    tools = [m for m in request["messages"] if m["role"] == "tool"]
                    assert len(tools) == 4
                    assert (tmp_path / "input.txt").read_text() == "state=AFTER\n"
                    assert (tmp_path / "nested/result.txt").read_text() == "state=AFTER\n"
                    assert not any("Error:" in str(t["content"]) for t in tools)
                    delta = {"role": "assistant", "content": "CODING_TOOLS_OK"}
                    reason = "stop"
                chunk = {
                    "id": "selected-offline",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
                final = {
                    **chunk,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                }
                body = (
                    "".join("data: " + json.dumps(v) + "\n\n" for v in (chunk, final))
                    + "data: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as error:
                failures.append(str(error))
                self.send_error(400, "Offline fixture assertion failed")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "canonical-offline"
    config.mkdir(mode=0o700)
    (config / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "selected-offline": {
                        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                        "api": "openai-completions",
                        "models": [
                            {
                                "id": "fixture",
                                "name": "Offline fixture",
                                "contextWindow": 32768,
                                "maxTokens": 2048,
                            }
                        ],
                    }
                }
            }
        )
    )
    (config / "auth.json").write_text(
        json.dumps({"selected-offline": {"type": "api_key", "key": "offline-only-fixture"}})
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    execution = SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=Path(package)
    )
    try:
        outcome = await asyncio.wait_for(execution.run(), 40)
        assert not failures
        assert len(requests) == 2
        assert outcome.response_message_id
        responses = [
            m for m in comms.views.full_history() if m.message_id == outcome.response_message_id
        ]
        assert len(responses) == 1 and responses[0].body == "CODING_TOOLS_OK"
        assert comms.registry.require("beta").active_turn is None
        assert not comms.bus.log.claim_projection()
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            snapshot = store.snapshot(execution.execution_id)
            assert snapshot.execution.lifecycle.completed
            assert (
                snapshot.attempt.lifecycle.backend_done and snapshot.attempt.lifecycle.process_dead
            )
        with pytest.raises(IdentityConflict, match="cannot be reused"):
            await execution.run()
        assert len(requests) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
