"""Fresh production bootstrap wakes a real native owner before and after restart."""

import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.coordination_store import MutationStore
from agent_comms.threads import Thread
from test_coordinated_runtime import tmp_path as private_root_fixture

# The native package executes against only the loopback model declared below.
tmp_path = private_root_fixture


@pytest.mark.parametrize("base_only_owner", [False, True])
def test_production_peer_wake_and_restart(tmp_path, monkeypatch, base_only_owner):
    package = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not package:
        pytest.skip("Prepared native package required")
    requests = []
    failures = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.headers["Authorization"] == "Bearer offline-only-fixture"
                assert request["model"] == "fixture"
                requests.append(request)
                assert len(requests) <= 2, "Restart or idle owner replayed an input"
                chunk = {
                    "id": f"bootstrap-{len(requests)}",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fixture",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": f"BOOTSTRAP_REPLY_{len(requests)}",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                }
                body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as error:
                failures.append(repr(error))
                self.send_error(400, "Loopback assertion failed")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    config = tmp_path / "pi"
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
                                "name": "Loopback fixture",
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
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "selected-offline/fixture")
    monkeypatch.setenv(
        "AGENT_COMMS_AGENT_ARGS", "--offline --no-extensions --no-skills --no-context-files"
    )
    for name in ("PI_PROMPT", "PI_PARENT_ID", "PI_AGENT_ID", "AGENT_COMMS_THREAD"):
        monkeypatch.delenv(name, raising=False)
    comms = Comms(tmp_path / "wire")
    for name in ("sender", "recipient"):
        comms.threads.register(
            Thread(
                name,
                frozenset(),
                str(tmp_path),
                model="selected-offline/fixture",
                thinking_level="off",
            )
        )
    # Ordinary publication must reach the same bootstrap as explicit protocol install.
    first = comms.messaging.send_message("sender", "recipient", "BOOTSTRAP_FIRST")
    with comms.bus.log.locked():
        root_id = comms.bus.log._private_marker_unlocked().root_id
    if base_only_owner:
        # Model the reported partial install on this disposable root only.
        for name in ("coordination.sqlite3", "native_prompt_bindings.sqlite3"):
            (comms.root / name).unlink()
        with MutationStore(str(comms.root / "coordination.sqlite3")):
            pass
    comms.owners.pin_private_nk_launch(comms.root, root_id, Path(package))

    def until(predicate):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            assert not failures, failures
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError(
            f"Native wake timed out; requests={requests!r}; "
            f"owner={comms.registry.require('recipient')!r}"
        )

    def replies():
        return [message for message in comms.views.full_history() if message.sender == "recipient"]

    try:
        comms.owners.start("recipient")
        until(lambda: len(replies()) == 1 and not comms.registry.require("recipient").executing)
        assert replies()[0].body == "BOOTSTRAP_REPLY_1" and replies()[0].target == "sender"
        original = comms.registry.require("recipient")
        with sqlite3.connect(comms.root / "coordination.sqlite3") as db:
            (session,) = db.execute("SELECT session_file FROM native_runtime_input").fetchone()
        assert Path(session).is_file()
        comms.owners.stop("recipient")
        assert not comms.registry.require("recipient").process_alive
        comms.owners.start("recipient")
        assert comms.registry.require("recipient").process_identity != original.process_identity
        second = comms.messaging.send_message("sender", "recipient", "BOOTSTRAP_SECOND")
        until(lambda: len(replies()) == 2 and not comms.registry.require("recipient").executing)
        assert replies()[1].body == "BOOTSTRAP_REPLY_2" and replies()[1].target == "sender"
        assert len(requests) == 2 and not failures
        for request, message in zip(requests, (first, second), strict=True):
            users = [item for item in request["messages"] if item["role"] == "user"]
            assert message.body in str(users[-1]["content"])
        # Persisted current native inputs prove both source deliveries survived reopen.
        with sqlite3.connect(comms.root / "coordination.sqlite3") as db:
            rows = db.execute(
                "SELECT input_id, session_entry_id FROM native_runtime_input"
            ).fetchall()
        assert len(rows) == 2 and all(entry for _, entry in rows)
        assert len({input_id for input_id, _ in rows}) == 2
    finally:
        comms.owners.stop("recipient")
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
