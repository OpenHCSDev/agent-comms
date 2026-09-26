"""Opt-in real native Pi + MCP SDK + ACP + optional actual Toad acceptance.

AC_MCP_NATIVE_BIN names an explicitly prepared native Pi launcher. No installation,
real credentials or external provider is used. AC_MCP_TOAD_ADAPTER optionally names
an owner-supplied module exporting async-context-manager open_observer(case, path).
Without that adapter these tests prove the Pi/ACP path, NOT Toad rendering.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_comms import backend
from agent_comms.acp import CommsAgent
from agent_comms.operations import wire
from agent_comms.runtime import RuntimeProxy

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY acceptance")
PACKAGE = Path(__file__).resolve().parents[1] / "extensions" / "pi-mcp-client"
TIMEOUT = 40


def _node_run(node, env, script, cwd):
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _prepare(root):
    node = shutil.which("node")
    assert node
    agent, project = root / "agent", root / "project"
    agent.mkdir()
    (project / ".pi").mkdir(parents=True)
    starts, calls = root / "server-starts", root / "server-calls"
    server = root / "fixture.mjs"
    sdk = PACKAGE / "node_modules" / "@modelcontextprotocol" / "sdk" / "dist" / "esm"
    zod = PACKAGE / "node_modules" / "zod" / "index.js"
    server.write_text(
        "import {appendFileSync} from 'node:fs';\n"
        f"import {{McpServer}} from {json.dumps((sdk / 'server/mcp.js').as_uri())};\n"
        f"import {{StdioServerTransport}} from {json.dumps((sdk / 'server/stdio.js').as_uri())};\n"
        f"import {{z}} from {json.dumps(zod.as_uri())};\n"
        f"appendFileSync({json.dumps(str(starts))}, String(process.pid)+'\\n');\n"
        "const server=new McpServer({name:'acceptance-fixture',version:'1.0.0'});\n"
        "server.registerTool('echo',{inputSchema:{message:z.string()}},async ({message})=>{\n"
        f"appendFileSync({json.dumps(str(calls))}, message+'\\n');\n"
        "return {content:[{type:'text',text:message}]};});\n"
        "await server.connect(new StdioServerTransport());\n"
    )
    declaration = {
        "id": "fixture",
        "enabled": True,
        "instructionsPolicy": "status-only",
        "transport": {"type": "stdio", "command": node, "args": [str(server)], "cwd": "project"},
    }
    document = json.dumps({"version": 1, "servers": [declaration]})
    (project / ".pi" / "mcp.json").write_text(document)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(root),
        "PI_CODING_AGENT_DIR": str(agent),
        "CI": "true",
        "NO_COLOR": "1",
        "PI_OFFLINE": "1",
        "AGENT_COMMS_AGENT_MODELS": "openrouter/z-ai/glm-5.3-flash",
    }
    api = PACKAGE / "node_modules/@earendil-works/pi-coding-agent/dist/index.js"
    _node_run(
        node,
        env,
        f"import {{ProjectTrustStore}} from {json.dumps(api.as_uri())};"
        f"new ProjectTrustStore({json.dumps(str(agent))}).set({json.dumps(str(project))},true);",
        project,
    )
    digest = _node_run(
        node,
        env,
        "import {declarationDigest,parseNativeConfig} from "
        f"{json.dumps((PACKAGE / 'src/config.mjs').as_uri())};"
        f"console.log(declarationDigest(parseNativeConfig({json.dumps(document)}).servers[0]));",
        project,
    )
    # Initial fixture setup uses the real package writer, never a Toad ledger.
    _node_run(
        node,
        env,
        "import {recordProjectDecision} from "
        f"{json.dumps((PACKAGE / 'src/ledger-write.mjs').as_uri())};"
        f"await recordProjectDecision({{agentDir:{json.dumps(str(agent))},"
        f"projectRoot:{json.dumps(str(project))},declaration:{json.dumps(declaration)},decision:'approve'}});",
        project,
    )
    (agent / "settings.json").write_text(
        json.dumps(
            {
                "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}},
                "compaction": {"enabled": False},
            }
        )
    )
    return node, agent, project, digest, starts, calls, env


def _deny_via_simulated_user_pty(node, project, digest, env, artifact):
    """Acceptance-owned simulated user; never production auto-confirmation."""
    master, slave = pty.openpty()
    process = None
    output = bytearray()
    try:
        process = subprocess.Popen(
            [
                node,
                str(PACKAGE / "bin/pi-mcp.mjs"),
                "trust",
                "deny",
                "--id",
                "fixture",
                "--digest",
                digest,
                "--project",
                str(project),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=project,
            env=env,
            close_fds=True,
        )
        os.close(slave)
        slave = -1
        deadline, typed = time.monotonic() + 15, False
        while time.monotonic() < deadline:
            if not select.select([master], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
            assert len(output) <= 65536
            if not typed and b"to apply:" in output:
                os.write(master, f"deny:fixture:{digest}\n".encode())
                typed = True
        assert typed
        assert process.wait(timeout=3) == 0, output.decode(errors="replace")
        rows = [
            json.loads(line)
            for line in output.decode().splitlines()
            if line.startswith('{"version":')
        ]
        assert rows[-1]["applied"] is True
    finally:
        artifact.write_bytes(output)
        os.close(master)
        if slave >= 0:
            os.close(slave)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=3)


@contextmanager
def _mock_model(root, agent, receipt_seen, second_request, release_final):
    requests, errors = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                size = int(self.headers["Content-Length"])
                assert 0 < size < 1000000
                request = json.loads(self.rfile.read(size))
                requests.append(request)
                assert len(requests) <= 2, "Unexpected model retry/round"
                assert receipt_seen.wait(12), "Native live receipt never reached ACP"
                if len(requests) == 1:
                    names = [tool["function"]["name"] for tool in request["tools"]]
                    name = next(name for name in names if name.startswith("mcp_fixture_echo_"))
                    delta = {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "echo-1",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps({"message": "ACCEPTANCE_ECHO"}),
                                },
                            }
                        ],
                    }
                    finish = "tool_calls"
                else:
                    assert any(row["role"] == "tool" for row in request["messages"])
                    second_request.set()
                    assert release_final.wait(12), "Midturn evidence gate was not released"
                    delta, finish = {"role": "assistant", "content": "ACCEPTANCE_DONE"}, "stop"
                chunk = {
                    "id": f"local-{len(requests)}",
                    "object": "chat.completion.chunk",
                    "created": 12345,
                    "model": "z-ai/glm-5.3-flash",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
                terminal = {
                    **chunk,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
                }
                body = (
                    "".join(f"data: {json.dumps(row)}\n\n" for row in [chunk, terminal])
                    + "data: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as error:
                errors.append(repr(error))
                self.send_error(500)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    (agent / "models.json").write_text(
        json.dumps({"providers": {"openrouter": {"baseUrl": origin + "/v1"}}})
    )
    guard = root / "local-only.cjs"
    guard.write_text(
        "const original=globalThis.fetch;globalThis.fetch=(url,...rest)=>{"
        "const link=url instanceof Request?url.url:String(url);"
        f"if(new URL(link).origin!=={json.dumps(origin)}) "
        "throw new Error('BLOCKED_NONLOCAL_NETWORK');"
        "return original(url,...rest);};"
    )
    try:
        yield requests, errors, guard
    finally:
        receipt_seen.set()
        release_final.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        (root / "model-requests.json").write_text(json.dumps(requests, indent=2))
        (root / "model-errors.json").write_text(json.dumps(errors))


@asynccontextmanager
async def _observer(case, root):
    path = os.environ.get("AC_MCP_TOAD_ADAPTER")
    if not path:
        yield None
        return
    spec = importlib.util.spec_from_file_location("mcp_toad_acceptance_adapter", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with module.open_observer(case, root) as observer:
        yield observer


@pytest.mark.parametrize("case", ["allow", "no_controller", "revoke_midturn", "disconnect"])
async def test_real_pi_mcp_acp_link(case, tmp_path, monkeypatch):
    binary = os.environ.get("AC_MCP_NATIVE_BIN")
    if not binary:
        pytest.skip("Set AC_MCP_NATIVE_BIN to an explicitly prepared native Pi launcher")
    assert Path(binary).is_absolute() and os.access(binary, os.X_OK)
    node, agent, project, digest, starts, calls, env = _prepare(tmp_path)
    receipt_seen, second_request, release_final = (threading.Event() for _ in range(3))
    if case != "revoke_midturn":
        release_final.set()
    attachment_settled = asyncio.Event()
    updates, permissions = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    task = proxy = owner = None
    (tmp_path / "launch.json").write_text(
        json.dumps(
            {
                "launcher": binary,
                "sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
                "package": str(PACKAGE),
                "case": case,
            }
        )
    )
    async with _observer(case, tmp_path) as observer:
        with _mock_model(tmp_path, agent, receipt_seen, second_request, release_final) as (
            requests,
            errors,
            guard,
        ):
            env.update(
                OPENROUTER_API_KEY="offline-fixture-no-real-key", NODE_OPTIONS=f"--require={guard}"
            )
            monkeypatch.setattr(backend.os, "environ", env)
            args = [
                "--offline",
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
                "--no-themes",
                "--no-context-files",
                "--no-builtin-tools",
                "--provider",
                "openrouter",
                "--model",
                "z-ai/glm-5.3-flash",
                "--thinking",
                "off",
                "-e",
                str(PACKAGE),
            ]
            owner = CommsAgent(
                wire(tmp_path / "wire"),
                agent_bin=binary,
                agent_args=args,
                runtime_enabled=True,
                auto_wake=False,
            )

            class Audit:
                async def session_update(self, session_id, update):
                    row = {
                        "sessionId": session_id,
                        "update": update.model_dump(by_alias=True, exclude_none=True),
                    }
                    updates.append(row)
                    if "mcpClient" in row["update"].get("_meta", {}).get("agentComms", {}):
                        receipt_seen.set()

            class Attachment:
                async def session_update(self, session_id, update):
                    if observer:
                        await observer.session_update(session_id=session_id, update=update)
                    meta = update.get("_meta", {}).get("agentComms", {})
                    if meta.get("turnSettled"):
                        attachment_settled.set()

                async def request_permission(self, **kwargs):
                    permissions.append(kwargs)
                    entered.set()
                    answer = (
                        await observer.request_permission(**kwargs)
                        if observer
                        else {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
                    )
                    await release.wait()
                    return answer

            owner.on_connect(Audit())  # Passive evidence sink, never a permission controller.
            try:
                await owner.new_session(cwd=str(project), mcp_servers=[])
                attached = SimpleNamespace(
                    _comms=owner._comms,
                    _transcript_snapshots=False,
                    _transcript_diffs=False,
                    _client=Attachment(),
                )
                proxy = RuntimeProxy(attached, "project", owner._runtime.path)
                await proxy.subscribe()
                if case == "no_controller":
                    context = owner._runtime.controller.set(None)
                    try:
                        task = asyncio.create_task(
                            owner.prompt(
                                "project", [{"type": "text", "text": "Run fixture echo once."}]
                            )
                        )
                    finally:
                        owner._runtime.controller.reset(context)
                else:
                    task = asyncio.create_task(
                        proxy.request(
                            "prompt", prompt=[{"type": "text", "text": "Run fixture echo once."}]
                        )
                    )
                    await asyncio.wait_for(entered.wait(), TIMEOUT)
                    assert owner._active_turns.get("project")
                    if case == "revoke_midturn":
                        await asyncio.to_thread(
                            _deny_via_simulated_user_pty,
                            node,
                            project,
                            digest,
                            env,
                            tmp_path / "pty-deny.log",
                        )
                        assert owner._active_turns.get("project")  # Genuine mid-turn denial.
                    elif case == "disconnect":
                        await proxy.close()  # Actual controlling Unix-socket attachment loss.
                        if observer:
                            await observer.disconnected()
                    release.set()
                    if case == "revoke_midturn":
                        assert await asyncio.to_thread(second_request.wait, 12)
                        assert owner._active_turns.get("project")
                        # The final model response is still held at localhost:
                        # connection retirement cannot be Pi turn shutdown.
                        for pid in map(int, starts.read_text().splitlines()):
                            with pytest.raises(ProcessLookupError):
                                os.kill(pid, 0)
                        (tmp_path / "midturn-retired.json").write_text(
                            json.dumps(
                                {
                                    "turnId": owner._active_turns["project"],
                                    "finalModelResponseHeld": True,
                                    "mcpPidsAbsent": True,
                                }
                            )
                        )
                        release_final.set()
                result = await asyncio.wait_for(task, TIMEOUT)
                if case != "disconnect":
                    await asyncio.wait_for(attachment_settled.wait(), 5)
                stop_reason = (
                    result.get("stopReason") if isinstance(result, dict) else result.stop_reason
                )
                assert stop_reason == "end_turn", updates
                assert not errors, errors
                assert len(requests) == 2
                assert len(permissions) == (0 if case == "no_controller" else 1)
                assert calls.exists() == (case == "allow")
                if calls.exists():
                    assert calls.read_text().splitlines() == ["ACCEPTANCE_ECHO"]
                active, receipts = None, []
                for row in updates:
                    assert row["sessionId"] == "project"
                    meta = row["update"].get("_meta", {}).get("agentComms", {})
                    if meta.get("turnStarted"):
                        active = meta["turnId"]
                    if "mcpClient" in meta:
                        assert active and meta["turnId"] == active
                        receipts.append(meta["mcpClient"])
                    if meta.get("turnSettled"):
                        assert meta["turnId"] == active
                        active = None
                assert active is None and len(receipts) == 1, updates
                assert receipts[0]["servers"] == [
                    {
                        "id": "fixture",
                        "scope": "project",
                        "state": "ready",
                        "calls": "confirm",
                        "tools": 1,
                        "resources": 0,
                        "prompts": 0,
                    }
                ]
                (tmp_path / "evidence.json").write_text(
                    json.dumps(
                        {
                            "case": case,
                            "toadAdapter": observer is not None,
                            "modelRequests": len(requests),
                            "permissionRequests": len(permissions),
                            "receiptCount": len(receipts),
                            "receiptBeforeSettlement": True,
                            "executedMcpCalls": 1 if calls.exists() else 0,
                            "controllingSocketDisconnected": case == "disconnect",
                            "midturnRetirement": case == "revoke_midturn",
                        },
                        indent=2,
                    )
                )
                if case == "revoke_midturn":
                    # Must already be reaped by package revalidation, BEFORE Pi shutdown.
                    for pid in map(int, starts.read_text().splitlines()):
                        with pytest.raises(ProcessLookupError):
                            os.kill(pid, 0)
            finally:
                release.set()
                release_final.set()
                receipt_seen.set()
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if proxy:
                    await proxy.close()
                await owner.shutdown()
                (tmp_path / "acp-updates.json").write_text(json.dumps(updates, indent=2))
                if starts.exists():
                    for pid in map(int, starts.read_text().splitlines()):
                        with pytest.raises(ProcessLookupError):
                            os.kill(pid, 0)
