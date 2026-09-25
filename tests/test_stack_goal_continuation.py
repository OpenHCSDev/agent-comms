"""Mounted /goal through ACP, the executing owner and the real native Pi.

By default only a localhost SSE provider is allowed. Set AC_GOAL_LIVE_PROVIDER
to opt into the already configured openai-codex/gpt-6-sol model explicitly.
"""

import asyncio
import json
import os
import shlex
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


@pytest.mark.asyncio
async def test_mounted_goal_continues_without_progress_tool(monkeypatch):
    native = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native or not os.environ.get("AC_TOAD_NATIVE_PILOT"):
        pytest.skip("Requires prepared native Pi and mounted Toad pilot dependencies")
    from runtime_fixture import ToadApp

    from agent_comms import wire

    live = bool(os.environ.get("AC_GOAL_LIVE_PROVIDER"))
    with TemporaryDirectory(prefix="ac-goal-continuation-", dir="/var/tmp") as raw:
        root = Path(raw)
        release = threading.Event()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                index = len(requests)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    if index == 2:
                        assert release.wait(20)
                    chunk = {
                        "id": f"goal-response-{index}",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "z-ai/glm-5.3-flash",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "One useful step."},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        model = "openai-codex/gpt-6-sol" if live else "openrouter/z-ai/glm-5.3-flash"
        provider, model_id = model.split("/", 1)
        native_args = [
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
            "--no-tools",
            "--provider",
            provider,
            "--model",
            model_id,
            "--thinking",
            "off",
        ]
        for key, value in {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "AGENT_COMMS_ROOT": str(root / "wire"),
            "AGENT_COMMS_AGENT_BIN": native,
            "AGENT_COMMS_AGENT_ARGS": shlex.join(native_args),
            "AGENT_COMMS_AGENT_MODELS": model,
        }.items():
            monkeypatch.setenv(key, value)
        if not live:
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
            monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
            monkeypatch.setenv("OPENROUTER_API_KEY", "local-only")
            monkeypatch.setenv("NODE_OPTIONS", f"--require={preload}")
        project = root / "project"
        project.mkdir()
        app = ToadApp(
            agent_data={
                "name": "Agent Comms",
                "identity": "goal-continuation-test",
                "short_name": "goal",
                "run_command": {"*": f"{sys.executable} -m agent_comms.acp"},
                "protocol": "acp",
            },
            project_dir=str(project),
        )
        comms = wire(root / "wire")

        async def until(predicate, timeout=45):
            async with asyncio.timeout(timeout):
                while not predicate():
                    await asyncio.sleep(0.025)

        def attempts():
            path = root / "wire/goal-private/goal_attempts.sqlite3"
            if not path.exists():
                return []
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                return db.execute(
                    "SELECT generation,phase,progress_witness FROM attempts ORDER BY generation"
                ).fetchall()

        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await until(
                    lambda: getattr(app.screen, "conversation", None) is not None
                    and app.screen.conversation.agent_ready
                )
                view = app.screen.conversation
                view.prompt.text = "/goal List one useful testing practice each turn until I pause."
                view.prompt.focus()
                await pilot.press("enter")
                await until(
                    lambda: len(attempts()) >= 2
                    or (
                        comms.registry.require("project").goal is not None
                        and comms.registry.require("project").goal.status == "blocked"
                    )
                )
                state = comms.registry.require("project").goal
                assert state.status == "active", state.progress
                rows = attempts()
                assert rows[0][1] == "succeeded" and rows[0][2].startswith("native-terminal:")
                assert rows[1][0] == 2
                if not live:
                    await until(lambda: len(requests) == 2)
                else:

                    def native_user_starts():
                        session = comms.registry.require("project").session_file
                        if not session or not Path(session).exists():
                            return 0
                        rows = [json.loads(line) for line in Path(session).read_text().splitlines()]
                        return sum(row.get("message", {}).get("role") == "user" for row in rows)

                    await until(lambda: native_user_starts() >= 2)
                    assert native_user_starts() == 2
                # Clear via the real UI route while the successor is in flight.
                # It revokes further grants without replaying or canceling that turn.
                await view.slash_command("/goal clear")
                release.set()
                await until(lambda: not comms.registry.require("project").executing)
                await asyncio.sleep(0.4)
                assert comms.registry.require("project").goal is None
                assert len(attempts()) == 2
                if not live:
                    assert len(requests) == 2
                assert app._exception is None
                print(f"GOAL_CONTINUATION_OK provider={provider} attempts={len(attempts())}")
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
