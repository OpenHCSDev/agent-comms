"""Real native Pi and owner socket: cancelled goals need an explicit fresh grant."""

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms import wire
from agent_comms.acp import CommsAgent
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.runtime import RuntimeProxy, socket_path


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")
async def test_native_cancelled_goal_resume_requires_retry_before_fresh_input(monkeypatch):
    native_bin = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native_bin:
        pytest.skip("Set AC_NATIVE_STACK_BIN to a prepared native launcher")
    with TemporaryDirectory(prefix="ac-native-resume-", dir="/var/tmp") as temporary:
        root = Path(temporary)
        agent_dir = root / "agent"
        agent_dir.mkdir(mode=0o700)
        release = threading.Event()
        finish_goal = threading.Event()
        requests = []
        first_chunk, continued = asyncio.Event(), asyncio.Event()
        updates = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                index = len(requests)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def emit(text, finish=None):
                    event = {
                        "id": f"retry-response-{index}",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "z-ai/glm-5.3-flash",
                        "choices": [
                            {"index": 0, "delta": {"content": text}, "finish_reason": finish}
                        ],
                    }
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()

                try:
                    emit("CANCELLED_RESPONSE" if index == 1 else "FRESH_RESPONSE")
                    gate = release if index == 1 else finish_goal
                    while not gate.wait(0.05):
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    emit("", "stop")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        (agent_dir / "models.json").write_text(
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
        for name in (
            "PI_AGENT_ID",
            "AGENT_COMMS_THREAD",
            "AGENT_COMMS_ROOT",
            "PI_SESSION_FILE",
            "PI_SESSION_ID",
            "PI_WORKTREE",
            "PI_TASK",
            "PI_PARENT_ID",
            "PI_AGENT_TAGS",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in {
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "OPENROUTER_API_KEY": "localhost-only",
            "PI_OFFLINE": "1",
            "NODE_OPTIONS": f"--require={preload}",
        }.items():
            monkeypatch.setenv(name, value)
        comms = wire(root / "wire")
        owner = CommsAgent(
            comms,
            agent_bin=native_bin,
            agent_args=[
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
            runtime_enabled=True,
            # Explicit prompts drive the two attempts; autonomous scheduling
            # is disabled so this regression has a strict two-request bound.
            auto_wake=False,
        )

        class Client:
            async def session_update(self, session_id, update):
                value = update.model_dump(by_alias=True, exclude_none=True)
                updates.append(value)
                text = value.get("content", {}).get("text", "")
                if "CANCELLED_RESPONSE" in text:
                    first_chunk.set()
                if "FRESH_RESPONSE" in text:
                    continued.set()

        owner.on_connect(Client())
        (root / "project").mkdir()
        session = (await owner.new_session(str(root / "project"))).session_id
        proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
        created = await proxy.request("set_goal", text="Continue the owner objective")
        goal_id = created["goal"]["id"]
        store = owner._open_goal_store()
        owner._dispositions.record(
            "acp:uncertain-sentinel",
            seq=None,
            owner=session,
            admission=1,
            target=session,
            text="UNCERTAIN_INPUT_MUST_NOT_REPLAY",
        )
        sentinel_before = owner._dispositions.get("acp:uncertain-sentinel")
        turn = asyncio.create_task(
            proxy.request(
                "prompt",
                prompt=[{"type": "text", "text": "ORIGINAL_CANCELLED_INPUT"}],
            )
        )
        fresh_turn = None
        try:
            await asyncio.wait_for(first_chunk.wait(), 15)
            claimed = store.snapshot(goal_id)
            assert claimed.state == "reserved"
            assert len(requests) == 1
            await asyncio.wait_for(proxy.request("cancel"), 10)
            assert (await asyncio.wait_for(turn, 10))["stopReason"] == "cancelled"
            release.set()
            paused = comms.registry.require(session).goal
            failed = GoalAttemptStore(store.root).snapshot(goal_id)
            assert paused.status == "paused"
            assert failed.state == "blocked" and failed.attempt_id == claimed.attempt_id
            # Resume cannot silently authorize replay of the cancelled attempt.
            with pytest.raises(RuntimeError, match="Retry"):
                await proxy.request(
                    "update_goal",
                    status="active",
                    goal_id=goal_id,
                    expected_revision=paused.revision,
                )
            blocked = comms.registry.require(session).goal
            assert blocked.status == "blocked"
            assert store.snapshot(goal_id) == failed
            assert len(requests) == 1
            retried = await proxy.request(
                "retry_goal", goal_id=goal_id, expected_revision=blocked.revision
            )
            assert retried["goal"]["status"] == "active"
            ready = store.snapshot(goal_id)
            assert ready.number == failed.number + 1 and ready.state == "ready"
            assert ready.attempt_id is None
            fresh_turn = asyncio.create_task(
                proxy.request(
                    "prompt",
                    prompt=[{"type": "text", "text": "FRESH_OWNER_INPUT"}],
                )
            )
            await asyncio.wait_for(continued.wait(), 15)
            fresh_claim = store.snapshot(goal_id)
            assert fresh_claim.state == "reserved"
            assert fresh_claim.attempt_id != failed.attempt_id
            current = comms.registry.require(session).goal
            await proxy.request(
                "update_goal", status="paused", goal_id=goal_id, expected_revision=current.revision
            )
            finish_goal.set()
            assert (await asyncio.wait_for(fresh_turn, 10))["stopReason"] == "end_turn"
            final = GoalAttemptStore(store.root).snapshot(goal_id)
            assert final.number == ready.number + 1 and final.state == "ready"
            assert len(requests) == 2
            assert owner._dispositions.get("acp:uncertain-sentinel") == sentinel_before
            assert "UNCERTAIN_INPUT_MUST_NOT_REPLAY" not in json.dumps(requests)
            disposition_rows = json.loads(owner._dispositions.path.read_text())["rows"].values()
            accepted = [row for row in disposition_rows if row["status"] == "started"]
            assert len(accepted) == 2
            assert len({row["native_id"] for row in accepted}) == 2
            native_session = Path(comms.registry.require(session).session_file)
            native_rows = [json.loads(line) for line in native_session.read_text().splitlines()]
            users = [
                row["message"]
                for row in native_rows
                if row.get("type") == "message" and row["message"].get("role") == "user"
            ]
            assert sum("ORIGINAL_CANCELLED_INPUT" in json.dumps(row) for row in users) == 1
            assert sum("FRESH_OWNER_INPUT" in json.dumps(row) for row in users) == 1
            assert not any(
                "[agent error]" in row.get("content", {}).get("text", "") for row in updates
            )
        finally:
            release.set()
            finish_goal.set()
            await owner.shutdown()
            await asyncio.gather(
                *(task for task in (turn, fresh_turn) if task), return_exceptions=True
            )
            await proxy.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
