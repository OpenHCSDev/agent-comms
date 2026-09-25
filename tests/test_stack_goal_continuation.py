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
@pytest.mark.parametrize(
    "empty_response,compact_tools", [(False, False), (True, False), (False, True)]
)
async def test_mounted_goal_continues_without_progress_tool(
    monkeypatch, empty_response, compact_tools
):
    native = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native or not os.environ.get("AC_TOAD_NATIVE_PILOT"):
        pytest.skip("Requires prepared native Pi and mounted Toad pilot dependencies")
    from runtime_fixture import ToadApp

    from agent_comms import wire

    live = bool(os.environ.get("AC_GOAL_LIVE_PROVIDER"))
    if live and (empty_response or compact_tools):
        pytest.skip("Only localhost provider can force the empty-response control")
    with TemporaryDirectory(prefix="ac-goal-continuation-", dir="/var/tmp") as raw:
        root = Path(raw)
        release = threading.Event()
        requests = []
        ordinary_requests = []
        summary_requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                index = len(requests)
                summary = compact_tools and requests[-1].get("reasoning", {}).get("effort") == "low"
                (summary_requests if summary else ordinary_requests).append(requests[-1])
                ordinary_index = len(ordinary_requests)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    if not summary and ordinary_index == (3 if compact_tools else 2):
                        assert release.wait(20)
                    chunk = {
                        "id": f"goal-response-{index}",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "z-ai/glm-5.3-flash",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "content": " \n\t" if empty_response else "One useful step."
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                    }
                    if compact_tools and not summary and ordinary_index == 1:
                        chunk["choices"][0] = {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": i,
                                        "id": f"read_{i}",
                                        "type": "function",
                                        "function": {
                                            "name": "read",
                                            "arguments": json.dumps(
                                                {"path": str(project / f"read-{i}.txt")}
                                            ),
                                        },
                                    }
                                    for i in range(3)
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                        chunk["usage"] = {
                            "prompt_tokens": 91349,
                            "completion_tokens": 3,
                            "total_tokens": 91352,
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
            "high" if compact_tools else "off",
        ]
        if compact_tools:
            native_args.remove("--no-tools")
            native_args.extend(["--tools", "read"])
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
                            "openrouter": {
                                "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                                "modelOverrides": {
                                    "z-ai/glm-5.3-flash": {
                                        "contextWindow": 128000,
                                        "maxTokens": 4096,
                                    }
                                },
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
            monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
            monkeypatch.setenv("OPENROUTER_API_KEY", "local-only")
            monkeypatch.setenv("NODE_OPTIONS", f"--require={preload}")
        project = root / "project"
        project.mkdir()
        if compact_tools:
            from test_stack_native_compaction import _saved_history

            (project / ".pi").mkdir()
            (project / ".pi/settings.json").write_text(
                json.dumps({"compaction": {"enabled": True}})
            )
            session = root / "saved.jsonl"
            _saved_history(session, project)
            saved = [json.loads(line) for line in session.read_text().splitlines()]
            for row in saved:
                if row.get("message", {}).get("role") == "assistant":
                    row["message"]["usage"].update(input=90000, totalTokens=90001)
            session.write_text("".join(json.dumps(row) + "\n" for row in saved))
            for index in range(3):
                (project / f"read-{index}.txt").write_text("abcdefghijklmno\n" * 2000)
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
                    lambda: (
                        getattr(app.screen, "conversation", None) is not None
                        and app.screen.conversation.agent_ready
                    )
                )
                view = app.screen.conversation
                if compact_tools:
                    comms.attach_session("project", str(session))
                view.prompt.text = "/goal List one useful testing practice each turn until I pause."
                view.prompt.focus()
                await pilot.press("enter")
                await until(
                    lambda: (
                        len(attempts()) >= 2
                        or (
                            comms.registry.require("project").goal is not None
                            and comms.registry.require("project").goal.status == "blocked"
                        )
                    )
                )
                state = comms.registry.require("project").goal
                if empty_response:
                    assert state.status == "blocked", state.progress
                    assert len(attempts()) == 1 and attempts()[0][1] == "failed"
                    await asyncio.sleep(0.4)
                    assert len(requests) == 1
                    return
                assert state.status == "active", state.progress
                rows = attempts()
                assert rows[0][1] == "succeeded" and rows[0][2].startswith("native-terminal:")
                assert rows[1][0] == 2
                if not live:
                    await until(lambda: len(ordinary_requests) == (3 if compact_tools else 2))
                    if compact_tools:
                        assert summary_requests
                        saved = [json.loads(line) for line in session.read_text().splitlines()]
                        assert sum(row.get("type") == "compaction" for row in saved) == 1
                        tool_results = [
                            row
                            for row in saved
                            if row.get("message", {}).get("role") == "toolResult"
                        ]
                        assert len(tool_results) == 3
                        assert all(not row["message"].get("isError") for row in tool_results)
                        assert all(
                            len(row["message"]["content"][0]["text"]) > 30000
                            for row in tool_results
                        )
                        # Summary requests belong to attempt one; they must not create
                        # extra goal attempts or replay its original user input.
                        assert (
                            sum(row.get("message", {}).get("inputId") is not None for row in saved)
                            == 2
                        )
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
                    assert len(ordinary_requests) == (3 if compact_tools else 2)
                assert app._exception is None
                print(f"GOAL_CONTINUATION_OK provider={provider} attempts={len(attempts())}")
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
