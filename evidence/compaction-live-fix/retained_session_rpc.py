"""Exercise actual Pi RPC and the user's retained session against loopback transport."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_comms.owner_compaction_prepare import _PREPARE

SOURCE = Path(
    "/home/ts/.pi/agent/sessions/--home-ts-code-projects-openhcs--/2026-09-22T03-50-07-145Z_01a0c73c-3628-7073-880c-e0df5f56e98e.jsonl"
)


async def run(package, root, expected):
    root.mkdir(mode=0o700)
    config = root / "config"
    config.mkdir()
    project = root / "project"
    project.mkdir()
    session = root / "retained.jsonl"
    before = SOURCE.stat()
    shutil.copyfile(SOURCE, session)
    session.chmod(0o600)
    after = SOURCE.stat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(
                {"bytes": len(json.dumps(data).encode()), "max_tokens": data.get("max_tokens")}
            )
            assert len(requests) <= 64
            chunk = {
                "id": "local-summary",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": "Retained-session transport verification summary.",
                        },
                        "finish_reason": None,
                    }
                ],
            }
            end = {
                **chunk,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            }
            body = (
                "".join("data: " + json.dumps(x) + "\n\n" for x in (chunk, end))
                + "data: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    provider = "retained-local"
    (config / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    provider: {
                        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                        "api": "openai-completions",
                        "models": [
                            {
                                "id": "fixture",
                                "name": "retained fixture",
                                "contextWindow": 272000,
                                "maxTokens": 8192,
                            }
                        ],
                    }
                }
            }
        )
    )
    (config / "auth.json").write_text(
        json.dumps({provider: {"type": "api_key", "key": "loopback-only"}})
    )
    (config / "settings.json").write_text(
        json.dumps(
            {"compaction": {"enabled": True, "reserveTokens": 16384, "keepRecentTokens": 20000}}
        )
    )
    fence = root / "local-only.cjs"
    fence.write_text(
        "const fetch=globalThis.fetch;globalThis.fetch=(url,...args)=>{const target=url instanceof Request?url.url:String(url);"
        + f'if(!target.startsWith("http://127.0.0.1:{server.server_port}/"))throw Error("NONLOCAL_NETWORK_REFUSED");'
        + "return fetch(url,...args)};"
    )
    env = {
        **os.environ,
        "PI_CODING_AGENT_DIR": str(config),
        "PI_OFFLINE": "1",
        "NODE_OPTIONS": f"--require={fence}",
    }
    stderr = (root / "native-stderr.log").open("wb")
    proc = await asyncio.create_subprocess_exec(
        "node",
        "--max-old-space-size=1536",
        str(package / "dist/cli.js"),
        "--mode",
        "rpc",
        "--offline",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-tools",
        "--provider",
        provider,
        "--model",
        "fixture",
        "--session",
        str(session),
        cwd=project,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
        limit=4 * 1024 * 1024,
    )

    async def rpc(command):
        proc.stdin.write((json.dumps(command) + "\n").encode())
        await proc.stdin.drain()
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), 120)
            if not line:
                raise RuntimeError((root / "native-stderr.log").read_text()[-3000:])
            data = json.loads(line)
            if data.get("id") == command["id"]:
                return data

    try:
        state = await rpc({"id": "state", "type": "get_state"})
        assert state.get("success"), state
        # Same installed native preparer, against the actual CLI-loaded copy.
        prep = subprocess.run(
            [
                "node",
                "--max-old-space-size=1536",
                "--input-type=module",
                "--eval",
                _PREPARE,
                str(package),
                str(session),
                "20000",
            ],
            cwd=project,
            env={**env, "NODE_OPTIONS": ""},
            capture_output=True,
            text=True,
            timeout=40,
            check=True,
        )
        preparation = json.loads(prep.stdout)
        command = {
            "id": "readiness",
            "type": "agent_comms_prepare_compaction",
            "version": 1,
            "dryRun": True,
            "witness": preparation["witness"],
            "selected": {"provider": provider, "modelId": "fixture", "contextWindow": 272000},
            "settings": {"reserveTokens": 16384, "keepRecentTokens": 20000},
        }
        readiness = await rpc(command)
        assert readiness.get("data", {}).get("status") == "ready", readiness
        operation = {k: v for k, v in command.items() if k != "dryRun"}
        operation.update(
            id="summary", type="agent_comms_summarize_compaction", operationId="e" * 32
        )
        response = await rpc(operation)
        data = response.get("data", {})
        receipt = {
            "package": str(package),
            "session_bytes": session.stat().st_size,
            "tokens_before": preparation["tokensBefore"],
            "readiness": readiness["data"]["status"],
            "summary_status": data.get("status"),
            "reason": data.get("reason"),
            "provider_calls": len(requests),
            "requests": requests,
            "transport": "loopback only",
            "native_commit_exercised": False,
        }
        (root / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt), flush=True)
        assert data.get("status") == expected, response
        if expected == "summarized":
            assert requests
        else:
            assert not requests and data.get("reason") == "limit_exceeded"
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 8)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        stderr.close()
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3]))
