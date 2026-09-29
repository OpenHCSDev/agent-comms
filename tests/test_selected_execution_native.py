"""SelectedExecution with pinned native Pi, local deterministic coding provider."""

import asyncio
import json
import os
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordination_cohort import accept_initial_cohort
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.native_pi import read_tracked_input_digest
from agent_comms.native_prompt_binding import read_expected_prompt_binding
from agent_comms.native_source_cursor import NativeSourceCursor
from agent_comms.selected_tool_broker import SelectedToolIntent
from test_coordinated_runtime import _root
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


@pytest.mark.parametrize(
    "after_cutover, selected_write", [(False, False), (True, False), (False, True)]
)
async def test_native_full_four_tools_publish_and_release(
    tmp_path, monkeypatch, after_cutover, selected_write
):
    package = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not package:
        pytest.skip("Prepared native package required; never build or call a paid provider")
    root, root_id, comms, initial, people = _root(tmp_path, direct=True, claims=True)
    old_seq = initial.message.seq
    if after_cutover:
        with comms.bus.log.locked():
            marker = comms.bus.log._private_marker_unlocked()
            marker.admission_after_seq = marker.last_seq
            comms.bus.log.write_metadata_unlocked(marker)
        fresh = comms.messaging.send_initial_cohort("sender", "beta", "Run the coding tools now.")
        initial = comms.bus.log.read_initial_cohort(root_id, fresh.seq)
        with Coordination(str(root / "coordination.sqlite3")) as store:
            accept_initial_cohort(comms.bus, root_id, fresh.seq, store)
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
                "command": (
                    "python -c \"from pathlib import Path; assert Path('input.txt').read_text() == "
                    "Path('nested/result.txt').read_text() == 'state=AFTER\\n'\""
                )
            },
        ),
    ]

    if selected_write:
        calls = [("selected_claimed_write", {"resource": "input.txt", "contents": "state=AFTER\n"})]

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
                        name for name, _ in calls
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
                    assert len(tools) == len(calls)
                    assert (tmp_path / "input.txt").read_text() == "state=AFTER\n"
                    if not selected_write:
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
        root=root,
        wire_root_id=root_id,
        owner_name="beta",
        native_package=Path(package),
        selected_tool_intent=SelectedToolIntent() if selected_write else None,
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
        if not selected_write:
            assert not comms.bus.log.claim_projection()
        with Coordination(str(root / "coordination.sqlite3")) as store:
            snapshot = store.snapshots.get(execution.execution_id)
            assert snapshot.execution.lifecycle.completed
            assert (
                snapshot.attempt.lifecycle.backend_done and snapshot.attempt.lifecycle.process_dead
            )
            assert execution.assignment.wire_seq == initial.message.seq
            cursor = NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(
                owner_name="beta"
            )
            assert cursor is not None
            assert cursor.injected_seq == initial.message.seq
            assert cursor.covered_seq >= cursor.injected_seq
            assert cursor.input_id == outcome.input_id
            proofs = read_historical_native_inputs(
                store,
                wire_root_id=root_id,
                recipient_lookup=stable_thread_lookup(owner.created_at),
                source_seq=initial.message.seq,
            )
            assert len(proofs) == 1 and proofs[0].input_id == outcome.input_id
            assert proofs[0].expected_prompt_equality_established
            binding = read_expected_prompt_binding(store, outcome.input_id)
            assert binding is not None
            assert (
                read_tracked_input_digest(proofs[0].context.session_file, outcome.input_id)
                == binding.expected_prompt_digest
            )
            if after_cutover:
                assert (
                    read_historical_native_inputs(
                        store,
                        wire_root_id=root_id,
                        recipient_lookup=stable_thread_lookup(owner.created_at),
                        source_seq=old_seq,
                    )
                    == ()
                )
        with pytest.raises(IdentityConflict, match="cannot be reused"):
            await execution.run()
        assert len(requests) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
