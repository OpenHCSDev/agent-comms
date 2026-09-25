"""Native Pi + streaming localhost provider: Send now interrupts, never replays."""

import asyncio
import json
import os
import sys
import threading
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms import backend


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface",
    ["backend", "backend_duplicate", "revoked", "acp", "acp_stopped", "toad", "toad_delayed"],
)
async def test_send_now_interrupts_native_response(surface, monkeypatch):
    if surface.startswith("toad") and not os.environ.get("AC_TOAD_NATIVE_PILOT"):
        pytest.skip("Opt-in mounted pilot needs Toad source/tests on PYTHONPATH and Python3.14")
    native_bin = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native_bin:
        pytest.skip("Set AC_NATIVE_STACK_BIN to a prepared pinned native launcher")
    with TemporaryDirectory(prefix="ac-send-now-", dir="/var/tmp") as raw:
        root = Path(raw)
        agent = root / "agent"
        agent.mkdir(mode=0o700)
        release = threading.Event()
        cancelled = threading.Event()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                index = len(requests)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def emit(text, finish=None):
                    row = {
                        "id": f"response-{index}",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "z-ai/glm-5.3-flash",
                        "choices": [
                            {"index": 0, "delta": {"content": text}, "finish_reason": finish}
                        ],
                    }
                    self.wfile.write(f"data: {json.dumps(row)}\n\n".encode())
                    self.wfile.flush()

                try:
                    emit("OLD_PARTIAL" if index == 1 else "NEW_FINAL")
                    if index == 1:
                        while not release.wait(0.05):
                            self.wfile.write(b": heartbeat\n\n")
                            self.wfile.flush()
                    emit("", "stop")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    cancelled.set()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        (agent / "models.json").write_text(
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
        queue = asyncio.Queue()
        events = []
        first_chunk = asyncio.Event()
        started = asyncio.Event()

        native_args = [
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
        ]
        child_env = {
            "PI_CODING_AGENT_DIR": str(agent),
            "OPENROUTER_API_KEY": "local-only",
            "PI_OFFLINE": "1",
            "NODE_OPTIONS": f"--require={preload}",
        }
        for key, value in child_env.items():
            monkeypatch.setenv(key, value)
        if surface.startswith("toad"):
            try:
                await _mounted_send_now(
                    root,
                    native_bin,
                    native_args,
                    requests,
                    cancelled,
                    monkeypatch,
                    surface == "toad_delayed",
                )
                assert not release.is_set()
            finally:
                release.set()
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)
            return
        owner = None
        if surface.startswith("acp"):
            from agent_comms import wire
            from agent_comms.acp import CommsAgent

            owner = CommsAgent(
                wire(root / "wire"),
                agent_bin=native_bin,
                agent_args=native_args,
                runtime_enabled=False,
                auto_wake=False,
            )

            class Client:
                async def session_update(self, session_id, update):
                    row = update.model_dump(by_alias=True, exclude_none=True)
                    events.append(row)
                    if "OLD_PARTIAL" in row.get("content", {}).get("text", ""):
                        first_chunk.set()
                    if (
                        row.get("_meta", {})
                        .get("agentComms", {})
                        .get("inputStarted", {})
                        .get("text")
                        == "URGENT_INPUT"
                    ):
                        started.set()

            owner.on_connect(Client())
            await owner.new_session(str(root / "project"))
            owner._drain_tasks["project"].cancel()
            await asyncio.gather(owner._drain_tasks["project"], return_exceptions=True)

        async def collect():
            if owner is not None:
                await owner._run_owned_input("project", "project", "ORIGINAL_INPUT")
                return
            async for event in backend.stream_agent_events(
                native_bin,
                native_args,
                "ORIGINAL_INPUT",
                str(root),
                env_extra={
                    "PI_CODING_AGENT_DIR": str(agent),
                    "OPENROUTER_API_KEY": "local-only",
                    "PI_OFFLINE": "1",
                    "NODE_OPTIONS": f"--require={preload}",
                },
                session_file=str(root / "session.jsonl"),
                steering_queue=queue,
                require_input_id=True,
                native_start=lambda *_: True,
                interrupt_boundary=lambda *_: nullcontext(surface != "revoked"),
            ):
                events.append(event)
                if event.get("type") == "chunk" and "OLD_PARTIAL" in event.get("text", ""):
                    first_chunk.set()
                if event.get("type") == "input_started" and event.get("id") == "urgent":
                    started.set()

        task = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(first_chunk.wait(), 15)
            command = {
                "type": "prompt",
                "message": "URGENT_INPUT",
                "streamingBehavior": "steer",
                "_input_id": "urgent",
            }
            if owner is None:
                await queue.put(command)
            else:
                await owner.prompt(
                    "project",
                    [{"type": "text", "text": "URGENT_INPUT"}],
                    agentComms={"deferDisplay": True},
                )
            await asyncio.sleep(0.2)
            assert len(requests) == 1 and not started.is_set(), "Queue waits for a boundary"
            if surface == "acp_stopped":
                owner._comms.registry.unregister("project")
            if owner is None:
                await queue.put({"type": "interrupt_steering", "_input_ids": ["urgent"]})
                if surface == "backend_duplicate":
                    await queue.put({"type": "interrupt_steering", "_input_ids": ["urgent"]})
            else:
                await owner.prompt(
                    "project", [{"type": "text", "text": " "}], agentComms={"sendNow": True}
                )
            if surface == "acp_stopped":
                await asyncio.wait_for(task, 10)
                assert len(requests) == 1 and not started.is_set()
                assert any(
                    row["status"] == "unknown" for row in owner._dispositions._read().values()
                )
                return
            if surface == "revoked":
                await asyncio.wait_for(task, 10)
                assert events[-1]["ok"] is False, events
                assert len(requests) == 1 and not started.is_set()
                assert queue.empty(), "Uncertain queued input must never be replayed"
                return
            await asyncio.wait_for(started.wait(), 3)
            await asyncio.wait_for(task, 10)
            assert not release.is_set(), "Send now must work while original response is unfinished"
            assert await asyncio.to_thread(cancelled.wait, 2), "Original request was not cancelled"
            assert len(requests) == 2, "No retry or duplicate prompt after explicit interruption"
            if owner is None:
                assert (
                    sum(
                        e.get("type") == "input_started" and e.get("id") == "urgent" for e in events
                    )
                    == 1
                )
                assert events[-1]["ok"] is True, events
                assert events[-1]["text"] == "NEW_FINAL", events
                assert not any(e.get("type") == "error" for e in events), events
            else:
                assert not owner._queued_inputs.get("project")
                assert all(
                    row["status"] == "started" for row in owner._dispositions._read().values()
                )
                assert not any(
                    "[agent error]" in row.get("content", {}).get("text", "") for row in events
                )
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if owner is not None:
                await owner.shutdown()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


async def _mounted_send_now(
    root, native_bin, native_args, requests, cancelled, monkeypatch, delayed
):
    import shlex

    from runtime_fixture import ToadApp
    from toad.widgets.agent_response import AgentResponse
    from toad.widgets.prompt import SendNow
    from toad.widgets.user_input import UserInput

    for key, value in {
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
        "AGENT_COMMS_ROOT": str(root / "wire"),
        "AGENT_COMMS_AGENT_BIN": native_bin,
        "AGENT_COMMS_AGENT_ARGS": shlex.join(native_args),
        "AGENT_COMMS_AGENT_MODELS": "openrouter/z-ai/glm-5.3-flash",
    }.items():
        monkeypatch.setenv(key, value)
    project = root / "project"
    project.mkdir()
    agent_data = {
        "name": "Agent Comms",
        "identity": "send-now-test",
        "short_name": "send-now",
        "run_command": {"*": f"{sys.executable} -m agent_comms.acp"},
        "protocol": "acp",
    }
    gate = threading.Event()
    if delayed:
        import toad.acp.agent as agent_module

        original_build = agent_module.build_prompt

        def prepare_prompt(project, prompt):
            if prompt == "URGENT_INPUT":
                assert gate.wait(5), "queued resource preparation was never released"
            return original_build(project, prompt)

        monkeypatch.setattr(agent_module, "build_prompt", prepare_prompt)
    app = ToadApp(agent_data=agent_data, project_dir=str(project))

    async def until(predicate, timeout=15):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.05)

    async with app.run_test(size=(120, 40)) as pilot:
        await until(
            lambda: getattr(app.screen, "conversation", None) is not None
            and app.screen.conversation.agent_ready
        )
        view = app.screen.conversation

        async def submit(text):
            view.prompt.text = text
            view.prompt.focus()
            await pilot.press("enter")

        await submit("ORIGINAL_INPUT")
        await until(
            lambda: any("OLD_PARTIAL" in block.source for block in view.query(AgentResponse))
        )
        await submit("URGENT_INPUT")
        await until(lambda: view.queued_prompts == ["URGENT_INPUT"])
        assert len(requests) == 1
        await pilot.click(SendNow)
        if delayed:
            await asyncio.sleep(0.2)
            assert len(requests) == 1
            gate.set()
        await until(
            lambda: any("NEW_FINAL" in block.source for block in view.query(AgentResponse)), 3
        )
        await until(lambda: not view.queued_prompts and view.busy_count == 0)
        assert await asyncio.to_thread(cancelled.wait, 2)
        assert len(requests) == 2
        assert sum("URGENT_INPUT" in block.content for block in view.query(UserInput)) == 1
        assert not any("[agent error]" in block.source for block in view.query(AgentResponse))
        assert app._exception is None
