"""Real retained Pi processes must reload the branch after manual compaction."""

import asyncio
import json
import os
import shlex
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from agent_comms import wire
from agent_comms.acp import CommsAgent


def _history(session: Path, project: Path) -> None:
    timestamp = "2026-09-24T00:00:00.000Z"
    rows = [
        {
            "type": "session",
            "version": 3,
            "id": str(uuid4()),
            "timestamp": timestamp,
            "cwd": str(project),
        }
    ]
    parent = None
    for index in range(12):
        for role in ("user", "assistant"):
            entry_id = f"{len(rows):08x}"
            message = {
                "role": role,
                "content": [
                    {
                        "type": "text",
                        "text": (
                            ("LEGACY_DISCARDED_HISTORY " if index == 0 else f"HISTORY_{index} ")
                            + "Recorded experimental observation. " * 400
                            if role == "user"
                            else "Recorded."
                        ),
                    }
                ],
                "timestamp": 1790290000000 + len(rows),
            }
            if role == "assistant":
                message.update(
                    api="openai-completions",
                    provider="openrouter",
                    model="z-ai/glm-5.3-flash",
                    stopReason="stop",
                    usage={
                        "input": 40000,
                        "output": 2,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "totalTokens": 40002,
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                            "total": 0,
                        },
                    },
                )
            rows.append(
                {
                    "type": "message",
                    "id": entry_id,
                    "parentId": parent,
                    "timestamp": timestamp,
                    "message": message,
                }
            )
            parent = entry_id
    session.write_text("".join(json.dumps(row) + "\n" for row in rows))
    session.chmod(0o600)


async def test_native_retained_child_reloads_manual_compaction(monkeypatch):
    native = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native:
        pytest.skip("Requires prepared native Pi stack")
    node = shutil.which("node")
    assert node is not None
    with TemporaryDirectory(prefix="ac-persistent-compact-", dir="/var/tmp") as directory:
        root = Path(directory)
        requests = []
        summary_entered = threading.Event()
        release_summary = threading.Event()
        summary = "COMPACTED_HISTORY_SENTINEL: Earlier experimental observations were recorded."

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                is_summary = len(requests) == 3
                if is_summary:
                    summary_entered.set()
                    assert release_summary.wait(15), "Test did not release native summary"
                chunk = {
                    "id": f"response-{len(requests)}",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "z-ai/glm-5.3-flash",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": summary if is_summary else "RECEIVED",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 40000,
                        "completion_tokens": 10,
                        "total_tokens": 40010,
                    },
                }
                body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        config = root / "agent"
        config.mkdir(mode=0o700)
        (config / "auth.json").write_text("{}")
        (config / "auth.json").chmod(0o600)
        (config / "settings.json").write_text(
            json.dumps(
                {
                    "compaction": {"enabled": False},
                    "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}},
                }
            )
        )
        # The compact runner clears NODE_OPTIONS and uses its own private profile.
        # A transparent node shim installs the localhost model config in that profile;
        # it executes the same installed native CLI and never replaces its RPC.
        preload = root / "local-only.cjs"
        preload.write_text(
            "const fs=require('node:fs');"
            f"const root={json.dumps(str(root))};"
            "if(process.argv.includes('--no-approve')){"
            "const prior=Number(fs.readFileSync(root+'/retained-pid','utf8'));"
            "let alive=true;try{process.kill(prior,0);}catch(e){"
            "if(e.code!=='ESRCH')throw e;alive=false;}"
            "fs.appendFileSync(root+'/writers.jsonl',JSON.stringify({pid:process.pid,"
            "prior,alive})+'\\n');if(alive)throw Error('RETAINED_CHILD_STILL_ALIVE');}"
            "const net=require('node:net');const connect=net.Socket.prototype.connect;"
            "net.Socket.prototype.connect=function(...args){"
            "const opts=Array.isArray(args[0])?args[0][0]:net._normalizeArgs(args)[0];"
            "if(opts.port && !['127.0.0.1','localhost','::1'].includes(opts.host))"
            "throw Error('BLOCKED_NONLOCAL_NETWORK: '+opts.host);"
            "return connect.apply(this,args);};"
            "fs.writeFileSync(process.env.PI_CODING_AGENT_DIR+'/models.json',"
            + json.dumps(
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
            + ");"
        )
        binaries = root / "bin"
        binaries.mkdir()
        shim = binaries / "node"
        shim.write_text(
            f'#!/bin/sh\nexec {shlex.quote(node)} --require={shlex.quote(str(preload))} "$@"\n'
        )
        shim.chmod(0o700)
        for key in (
            "PI_AGENT_ID",
            "PI_PARENT_ID",
            "PI_WORKTREE",
            "AGENT_COMMS_THREAD",
            "AGENT_COMMS_MANAGED",
            "AGENT_COMMS_DEBUG_LOG",
        ):
            monkeypatch.delenv(key, raising=False)
        for key, value in {
            "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
            "PI_CODING_AGENT_DIR": str(config),
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
            "OPENROUTER_API_KEY": "local-only",
            "NODE_OPTIONS": "",
            "AGENT_COMMS_ROOT": str(root / "wire"),
            "AGENT_COMMS_AGENT_MODELS": "openrouter/z-ai/glm-5.3-flash",
        }.items():
            monkeypatch.setenv(key, value)
        project = root / "worker"
        project.mkdir()
        session = root / "saved.jsonl"
        _history(session, project)
        comms = wire(root / "wire")
        owner = CommsAgent(
            comms,
            agent_bin=native,
            agent_args=[
                "--provider",
                "openrouter",
                "--model",
                "z-ai/glm-5.3-flash",
                "--thinking",
                "off",
            ],
            runtime_enabled=False,
            auto_wake=False,
        )
        updates = []

        class Client:
            async def session_update(self, **kwargs):
                updates.append(kwargs["update"].model_dump(by_alias=True))

        owner.on_connect(Client())
        retained = resumed = None
        compact_task = None
        try:
            await owner.new_session(str(project))
            comms.attach_session("worker", str(session))
            await asyncio.wait_for(owner._run_owned_input("worker", "worker", "WARMUP"), 20)
            retained = owner._persistent_backends["worker"].proc
            assert retained is not None and retained.returncode is None, json.dumps(
                updates, indent=2
            )
            await asyncio.wait_for(owner._run_owned_input("worker", "worker", "REUSE"), 20)
            assert owner._persistent_backends["worker"].proc is retained
            assert retained.returncode is None
            assert len(requests) == 2
            assert "LEGACY_DISCARDED_HISTORY" in json.dumps(requests[-1]["messages"])
            before = session.read_bytes()
            (root / "retained-pid").write_text(str(retained.pid))
            compact_task = asyncio.create_task(owner.compact_context("worker"))
            entered = await asyncio.to_thread(summary_entered.wait, 15)
            if not entered and compact_task.done():
                pytest.fail(f"Native compaction did not request summary: {compact_task.result()}")
            assert entered, "Native compaction did not reach localhost summary"
            assert retained.returncode is not None
            assert owner._persistent_backends["worker"].proc is None
            writers = [
                json.loads(line) for line in (root / "writers.jsonl").read_text().splitlines()
            ]
            assert len(writers) == 1
            assert writers[0]["prior"] == retained.pid
            assert writers[0]["alive"] is False
            assert session.read_bytes() == before
            assert "LEGACY_DISCARDED_HISTORY" in json.dumps(requests[2]["messages"])
            release_summary.set()
            result = await asyncio.wait_for(compact_task, 20)
            assert result["ok"] is True, result
            rows = [json.loads(line) for line in session.read_text().splitlines()]
            compactions = [row for row in rows if row.get("type") == "compaction"]
            assert len(compactions) == 1
            assert summary in compactions[0]["summary"]
            await asyncio.wait_for(owner._run_owned_input("worker", "worker", "AFTER_COMPACT"), 20)
            resumed = owner._persistent_backends["worker"].proc
            assert resumed is not None and resumed.returncode is None
            assert resumed.pid != retained.pid
            assert len(requests) == 4
            context = json.dumps(requests[-1]["messages"])
            assert "COMPACTED_HISTORY_SENTINEL" in context
            assert "AFTER_COMPACT" in context
            assert "LEGACY_DISCARDED_HISTORY" not in context
            await owner.shutdown()
            assert resumed.returncode is not None
            assert not owner._persistent_backends
        finally:
            release_summary.set()
            if compact_task is not None and not compact_task.done():
                compact_task.cancel()
                await asyncio.gather(compact_task, return_exceptions=True)
            await owner.shutdown()
            for process in (retained, resumed):
                if process is not None:
                    assert process.returncode is not None
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)
