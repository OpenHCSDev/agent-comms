"""A published native reply automatically reaches the original channel sender."""

import asyncio
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_comms.native_input_record import TriageNativeExecution, FullNativeExecution
from agent_comms.selected_triage import IgnoreSelectedTriage
from agent_comms.acp import CommsClient
from agent_comms.acp_extension import (
    CursorAdvancedUpdate,
    VerifiedCursorObservation,
    decode_updates,
)
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.comms import Comms
from agent_comms.private_bus_checkpoint import addressed_source_pointers_unlocked
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from test_coordinated_runtime import tmp_path as private_root_fixture

# Reuse the existing sealed-runtime persistent /var/tmp test root.
tmp_path = private_root_fixture


@pytest.mark.parametrize(
    ("restart_after_reply", "contend_cursor"),
    [(False, False), (True, False), (False, True)],
    ids=["reply", "saved-restart", "contended-reply"],
)
async def test_native_channel_reply_automatically_reaches_original_sender(
    tmp_path, monkeypatch, restart_after_reply, contend_cursor
):
    pin = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
    if not pin:
        pytest.skip("Actual immutable native bundle required")
    package = Path(pin).resolve(strict=True)
    requests = []
    failures = []
    seed_count = 2 if restart_after_reply else 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.headers["Authorization"] == "Bearer local-only"
                requests.append(request)
                assert len(requests) <= 2 + seed_count + restart_after_reply, "Reply observation caused replay or ping-pong"
                text = (
                    '{"decision":"IGNORE"}'
                    if "bounded triage" in json.dumps(request)
                    else "ROUNDTRIP_NATIVE_REPLY"
                )
                chunk = {
                    "id": f"roundtrip-{len(requests)}",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                }
                body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if contend_cursor and len(requests) == 2:
                    # Return the real model result while another reader holds
                    # the bus lock across the worker's cursor projection.
                    with _store_lock(comms.bus.log.path):
                        self.wfile.write(body)
                        self.wfile.flush()
                        time.sleep(0.8)
                else:
                    self.wfile.write(body)
            except Exception as error:
                failures.append(repr(error))
                self.send_error(400, "Local fixture assertion failed")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    (config / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "roundtrip-local": {
                        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                        "api": "openai-completions",
                        "models": [
                            {
                                "id": "fixture",
                                "name": "Local roundtrip",
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
        json.dumps(
            {
                "roundtrip-local": {
                    "type": "api_key",
                    "key": "local-only",
                }
            }
        )
    )
    (config / "settings.json").write_text(
        json.dumps(
            {
                "compaction": {"enabled": False},
                "retry": {"enabled": False, "maxRetries": 0},
            }
        )
    )
    for key, value in {
        "PI_CODING_AGENT_DIR": str(config),
        "AGENT_COMMS_NATIVE_CONFIG_DIR": str(config),
        "AGENT_COMMS_AGENT_MODELS": "roundtrip-local/fixture",
        "AGENT_COMMS_AGENT_ARGS": "--offline --no-extensions --no-skills --no-context-files",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("PI_PROMPT", "PI_PARENT_ID", "PI_AGENT_ID", "AGENT_COMMS_THREAD"):
        monkeypatch.delenv(key, raising=False)
    comms = Comms(tmp_path / "wire")
    projects = {}
    for name in ("questioner", "answerer"):
        project = tmp_path / name
        project.mkdir()
        projects[name] = project
        comms.registry.declare(
            Thread(
                name,
                frozenset({"team"}),
                str(project),
                model="roundtrip-local/fixture",
                thinking_level="off",
            )
        )
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.owners.pin_private_nk_launch(comms.root, root_id, package)
    attachment = CommsClient(
        comms,
        runtime_enabled=True,
        private_nk_native_package=package,
        private_nk_wire_root_id=root_id,
    )
    facts = []

    class Client:
        async def session_update(self, **kwargs):
            facts.extend(decode_updates(kwargs["update"].get("_meta")))

    attachment.on_connect(Client())

    async def until(predicate, description):
        try:
            async with asyncio.timeout(25):
                while not predicate():
                    assert not failures, failures
                    await asyncio.sleep(0.05)
        except TimeoutError:
            print("TIMEOUT", description, "ACP facts", repr(facts), flush=True)
            with sqlite3.connect(comms.root / "coordination.sqlite3") as db:
                print(
                    "native receipts",
                    db.execute("SELECT * FROM native_runtime_input").fetchall(),
                    flush=True,
                )
            raise
        print(description, "provider_calls", len(requests), flush=True)

    try:
        for name in projects:
            await asyncio.to_thread(comms.owners.start, name)
        await attachment.load_session(cwd=str(projects["questioner"]), session_id="questioner")
        if restart_after_reply:
            for name, project in projects.items():
                await attachment.load_session(cwd=str(project), session_id=name)
                async with asyncio.timeout(35):
                    seed = await attachment.prompt(
                        session_id=name,
                        prompt=[{"type": "text", "text": f"RETAINED_NATIVE_HISTORY_{name}"}],
                    )
                assert seed.stop_reason == "end_turn"
                assert comms.registry.require(name).session_file
            assert len(requests) == seed_count
        facts.clear()
        question = comms.messaging.send_message(
            "questioner", "#team", "@answerer ROUNDTRIP_QUESTION"
        )
        await until(lambda: len(requests) >= seed_count + 1, "B actual native request")
        await until(
            lambda: len(comms.views.channel_history("#team")) >= 2, "B published actual reply"
        )
        reply = comms.views.channel_history("#team")[-1]
        assert reply.sender == "answerer" and reply.body == "ROUNDTRIP_NATIVE_REPLY"
        # No user prompt, explicit inbox drain, participant insertion, or selected claim seeding.
        try:
            await until(lambda: len(requests) == seed_count + 2, "A automatically observed native reply")
        except TimeoutError:
            print(
                "RED: native reply published but sender never automatically received it",
                "question",
                question.seq,
                "reply",
                reply.seq,
                "facts",
                repr(facts),
                flush=True,
            )
            raise
        users = [item for item in requests[seed_count + 1]["messages"] if item["role"] == "user"]
        assert reply.body in json.dumps(users[-1]["content"])
        # The same response remains available through canonical natural-turn awareness.
        # Reading this projection proves neither model delivery nor ACK; native delivery
        # above must have happened automatically before inspecting pointers.
        owner = comms.registry.require("questioner")
        with comms.bus.log.locked():
            marker = comms.bus.log._private_marker_unlocked()
            pointers = addressed_source_pointers_unlocked(
                comms.bus.log, marker, stable_thread_lookup(owner.created_at)
            )
        assert reply.message_id in [pointer.message_id for pointer in pointers]

        await until(
            lambda: any(
                isinstance(fact, CursorAdvancedUpdate)
                and isinstance(fact.envelope.observation, VerifiedCursorObservation)
                and fact.envelope.observation.cursor.injected_seq == reply.seq
                for fact in facts
            ),
            "A ACP feedback proves exact reply injection",
        )
        with sqlite3.connect(comms.root / "coordination.sqlite3") as db:
            rows = db.execute(
                "SELECT input_id, session_file, session_entry_id FROM native_runtime_input"
            ).fetchall()
        delivered = [
            fact.envelope.observation.cursor
            for fact in facts
            if isinstance(fact, CursorAdvancedUpdate)
            if isinstance(fact.envelope.observation, VerifiedCursorObservation)
            if fact.envelope.observation.cursor.injected_seq == reply.seq
        ]
        assert delivered[-1].covered_seq == reply.seq
        assert delivered[-1].stage is TriageNativeExecution
        assert delivered[-1].input_id in {row[0] for row in rows}
        assert len(rows) == 2 and all(row[2] for row in rows)
        assert len({row[0] for row in rows}) == 2
        assert len({row[1] for row in rows}) == 2  # Two actual native owners/children.
        await asyncio.sleep(1.2)  # Beyond the watcher fallback interval; no recursive wake/replay.
        assert len(requests) == seed_count + 2 and not failures
        assert len(comms.views.channel_history("#team")) == 2
        if restart_after_reply:
            # Continue the same real owner/ACP/native journey from saved state.
            await until(
                lambda: all(not comms.registry.require(name).executing for name in projects),
                "Both native turns released before guarded restart",
            )
            before = comms.registry.snapshot()
            saved = {
                name: Path(before.threads[name].session_file).read_bytes() for name in projects
            }
            await attachment.shutdown()
            await asyncio.to_thread(comms.owners.restart_owners, list(projects))
            after = comms.registry.snapshot()
            for name in projects:
                old, current = before.threads[name], after.threads[name]
                assert current.incarnation == old.incarnation
                assert current.process_identity != old.process_identity
                assert after.admission_generations[name] > before.admission_generations[name]
                assert current.session_file == old.session_file
                assert Path(current.session_file).read_bytes() == saved[name]
            assert len(requests) == seed_count + 2, "Restart replayed saved input"
            attachment = CommsClient(
                comms, runtime_enabled=True, private_nk_native_package=package,
                private_nk_wire_root_id=root_id,
            )
            attachment.on_connect(Client())
            await attachment.load_session(cwd=str(projects["questioner"]), session_id="questioner")
            async with asyncio.timeout(35):
                answer = await attachment.prompt(
                    session_id="questioner",
                    prompt=[{"type": "text", "text": "EXPLICIT_POST_RESTART_INPUT"}],
                )
            assert answer.stop_reason == "end_turn"
            assert len(requests) == seed_count + 3
            assert not failures, failures
            entries = [json.loads(line) for line in Path(after.threads['questioner'].session_file).read_text().splitlines()]
            inputs = [row['message'] for row in entries if row['type'] == 'message' and row['message']['role'] == 'user']
            assert sum('EXPLICIT_POST_RESTART_INPUT' in json.dumps(row['content']) for row in inputs) == 1
            assert Path(after.threads['questioner'].session_file).read_bytes().startswith(saved['questioner'])
    finally:
        await attachment.shutdown()
        for name in projects:
            await asyncio.to_thread(comms.owners.stop, name)
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
        assert all(not comms.registry.require(name).process_alive for name in projects)
