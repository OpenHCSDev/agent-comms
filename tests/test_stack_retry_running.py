"""Real native Pi and owner socket: Retry waits for an unrelated active response."""

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
async def test_native_retry_waits_for_current_response_without_replaying_unknown(monkeypatch):
    native_bin = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native_bin:
        pytest.skip("Set AC_NATIVE_STACK_BIN to a prepared native launcher")
    with TemporaryDirectory(prefix="ac-native-retry-", dir="/var/tmp") as temporary:
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
                    emit("ORDINARY_RESPONSE" if index == 1 else "GOAL_CONTINUED")
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
        )

        class Client:
            async def session_update(self, session_id, update):
                value = update.model_dump(by_alias=True, exclude_none=True)
                updates.append(value)
                text = value.get("content", {}).get("text", "")
                if "ORDINARY_RESPONSE" in text:
                    first_chunk.set()
                if "GOAL_CONTINUED" in text:
                    continued.set()

        owner.on_connect(Client())
        (root / "project").mkdir()
        session = (await owner.new_session(str(root / "project"))).session_id
        store = owner._open_goal_store()
        goal = comms.update_goal(
            session, "set", text="Finish the blocked objective", owner_store=store
        )
        reservation = store.reserve(goal.id, 1)
        store.claim_launch(reservation)
        store.record_failed(reservation, "Earlier goal attempt failed")
        blocked = comms.update_goal(session, "blocked", goal_id=goal.id)
        owner._dispositions.record(
            "acp:old-unknown",
            seq=None,
            owner=session,
            admission=1,
            target=session,
            text="UNCERTAIN_OLD_INPUT_MUST_NOT_REPLAY",
        )
        unknown_before = owner._dispositions.get("acp:old-unknown")
        proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
        turn = asyncio.create_task(
            proxy.request(
                "prompt",
                prompt=[{"type": "text", "text": "CURRENT_USER_REQUEST"}],
            )
        )
        try:
            await asyncio.wait_for(first_chunk.wait(), 15)
            assert session in owner._active_turns and session in owner._backend_inboxes
            result = await proxy.request(
                "retry_goal",
                goal_id=goal.id,
                expected_revision=blocked.revision,
            )
            assert result["goal"]["status"] == "active"
            generation = GoalAttemptStore(store.root).snapshot(goal.id)
            assert (generation.number, generation.state, generation.attempt_id) == (
                2,
                "ready",
                None,
            )
            await asyncio.sleep(0.25)
            assert len(requests) == 1 and not continued.is_set()
            assert store.snapshot(goal.id) == generation
            release.set()
            assert (await asyncio.wait_for(turn, 10))["stopReason"] == "end_turn"
            await asyncio.wait_for(continued.wait(), 15)
            # Pause before releasing the second response so another live-drain
            # iteration cannot reserve a third attempt between success and pause.
            assert session in owner._active_turns and len(requests) == 2
            current = comms.registry.require(session).goal
            await proxy.request(
                "update_goal",
                status="paused",
                goal_id=goal.id,
                expected_revision=current.revision,
            )
            assert comms.registry.require(session).goal.status == "paused"
            finish_goal.set()
            await asyncio.wait_for(asyncio.shield(owner._wake_tasks[session]), 10)
            final = GoalAttemptStore(store.root).snapshot(goal.id)
            assert (final.number, final.state, final.attempt_id) == (3, "ready", None)
            assert len(requests) == 2, "One ordinary request and one authorized goal continuation"
            assert owner._dispositions.get("acp:old-unknown") == unknown_before
            assert "UNCERTAIN_OLD_INPUT_MUST_NOT_REPLAY" not in json.dumps(requests)
            assert "CURRENT_USER_REQUEST" in json.dumps(requests[0])
            assert "Finish the blocked objective" in json.dumps(requests[1])
            assert not any(
                "[agent error]" in row.get("content", {}).get("text", "") for row in updates
            )
        finally:
            release.set()
            finish_goal.set()
            await owner.shutdown()
            await asyncio.gather(turn, return_exceptions=True)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
