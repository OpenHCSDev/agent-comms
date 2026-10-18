"""Opt-in real native Pi + MCP SDK + ACP + optional actual Toad acceptance.

AC_MCP_NATIVE_BIN names an explicitly prepared native Pi launcher. No installation,
real credentials or external provider is used. AC_MCP_TOAD_ADAPTER optionally names
an owner-supplied module exporting async-context-manager open_observer(case, path).
Without that adapter these tests prove the Pi/ACP path, NOT Toad rendering.
"""

from __future__ import annotations

import asyncio
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
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_comms.acp_extension import (
    McpClientReceiptUpdate,
    TurnChangedUpdate,
    decode_updates,
)
from agent_comms.comms import wire
from agent_comms.child_process import ParentedProcess
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.runtime import RuntimeProxy
from delivery_owner_fixture import canonical_agent

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY acceptance")
TIMEOUT = 40


def _node_run(node, env, script, cwd, artifact):
    native_package = Path(env["PI_COMPACTION_TEST_PACKAGE"])
    command = [
        node, "--no-global-search-paths",
        "--import", str(native_package / "dist/agent-comms-import-fence.mjs"),
        "--import", str(native_package / "dist/agent-comms-project-bootstrap.mjs"),
        "--input-type=module", "-e", script,
    ]
    child = outcome = None
    stdout = stderr = b""
    error = None
    try:
        with ParentedProcess.launch(tuple(command), cwd=cwd, env=env, output=subprocess.PIPE) as child:
            try:
                stdout, stderr = child.process.communicate(timeout=15)
                outcome = child.reap()
            except subprocess.TimeoutExpired as failure:
                stdout, stderr = failure.stdout or b"", failure.stderr or b""
                raise
    except BaseException as failure:
        error = repr(failure)
        raise
    finally:
        artifact.with_suffix(".stdout").write_bytes(stdout)
        artifact.with_suffix(".stderr").write_bytes(stderr)
        artifact.with_suffix(".command.json").write_text(json.dumps({
            "argv": command, "cwd": str(cwd),
            "identity": FieldCodec.encode(child.identity) if child else None,
            "outcome": FieldCodec.encode(outcome) if outcome else None,
            "error": error, "output_complete": outcome is not None,
            "retired": child.retired if child else None,
            "remaining_groups": [FieldCodec.encode(member) for member in
                                 child.platform.group_members(child.identity)] if child else [],
        }, indent=2))
    assert outcome.successful, stderr.decode(errors="replace")
    return stdout.decode().strip()


def _qualify_origin(node, env, project, artifact):
    """Reach the actual SDK cause before dispatch, using only loopback addresses."""
    native_package = Path(env["PI_COMPACTION_TEST_PACKAGE"])
    allowed = env["AGENT_COMMS_NATIVE_ORIGIN"]
    # A different scheme is outside the exact allowed origin. Even a broken
    # guard must not send this authored control toward an external network.
    requested = "https:" + allowed.removeprefix("http:")
    script = "\n".join((
        "import assert from 'node:assert/strict';",
        "import {channel} from 'node:diagnostics_channel';",
        f"const allowed={json.dumps(allowed)},requested={json.dumps(requested)};",
        "const dispatched=[], report={allowed_origin:allowed,requested_origin:requested};",
        "let firstDispatcher;",
        "const requests=channel('undici:request:create');",
        "const observe=({request})=>dispatched.push({origin:new URL(String(request.origin)).origin,path:request.path});",
        "requests.subscribe(observe);",
        f"const {{configureHttpDispatcher}}=await import({json.dumps((native_package / 'dist/core/http-dispatcher.js').as_uri())});",
        f"const {{getGlobalDispatcher}}=await import({json.dumps((native_package / 'node_modules/undici/index.js').as_uri())});",
        "try {",
        "  await configureHttpDispatcher();",
        "  firstDispatcher=getGlobalDispatcher();",
        "  const firstFetch=globalThis.fetch;",
        "  const positive=await fetch(allowed+'/guard-positive');",
        "  assert.equal(positive.status,200); await positive.text();",
        "  assert(dispatched.some(row=>row.origin===allowed && row.path==='/guard-positive'));",
        f"  const {{OpenAI}}=await import({json.dumps((native_package / 'node_modules/openai/client.mjs').as_uri())});",
        "  const client=new OpenAI({baseURL:requested+'/v1',apiKey:'offline-fixture-no-real-key',maxRetries:0});",
        "  await configureHttpDispatcher();",
        "  assert.notEqual(globalThis.fetch,firstFetch);",
        "  assert.notEqual(getGlobalDispatcher(),firstDispatcher);",
        "  report.dispatcher_reconfigured=true; report.refusals=[];",
        "  const replacement=new OpenAI({baseURL:requested+'/v1',apiKey:'offline-fixture-no-real-key',maxRetries:0});",
        "  for (const sdk of [client,replacement]) {",
        "    let failure;",
        "    try { await sdk.chat.completions.create({model:'z-ai/glm-5.3-flash',messages:[{role:'user',content:'Authored origin refusal; never dispatched.'}]}); }",
        "    catch(error) { failure=error; report.refusals.push({name:error.name,message:error.message,cause:{code:error.cause?.code,allowed_origin:error.cause?.allowed_origin,requested_origin:error.cause?.requested_origin}}); }",
        "    assert.equal(failure?.cause?.code,'ERR_AGENT_COMMS_NATIVE_ORIGIN_REFUSED');",
        "    assert.equal(failure.cause.allowed_origin,allowed);",
        "    assert.equal(failure.cause.requested_origin,requested);",
        "  }",
        "  assert(!dispatched.some(row=>row.origin!==allowed));",
        "  let redirected=false;",
        "  try { const response=await fetch(allowed+'/guard-redirect'); await response.text(); redirected=true; }",
        "  catch(error) { report.redirect_error={name:error.name,message:error.message,cause_message:error.cause?.message}; }",
        "  assert.equal(redirected,false);",
        "  assert(report.redirect_error);",
        "  assert(dispatched.some(row=>row.origin===allowed && row.path==='/guard-redirect'));",
        "  assert(!dispatched.some(row=>row.origin!==allowed));",
        "  report.localhost_positive=true; report.sdk_origin_refused=true; report.redirect_refused=true;",
        "} finally {",
        "  report.dispatched=dispatched; console.log(JSON.stringify(report));",
        "  requests.unsubscribe(observe);",
        "  try { if(firstDispatcher) await firstDispatcher.close(); }",
        "  finally { if(getGlobalDispatcher()!==firstDispatcher) await getGlobalDispatcher().close(); }",
        "}",
    ))
    report = json.loads(_node_run(node, env, script, project, artifact))
    artifact.with_suffix(".json").write_text(json.dumps(report, indent=2))


def _prepare(root, native_package):
    package = native_package / "agent-comms-extensions" / "pi-mcp-client"
    node = shutil.which("node")
    assert node
    agent, project = root / "agent", root / "project"
    agent.mkdir()
    (project / ".pi").mkdir(parents=True)
    starts, calls = root / "server-starts", root / "server-calls"
    server = root / "fixture.mjs"
    sdk = package / "node_modules" / "@modelcontextprotocol" / "sdk" / "dist" / "esm"
    zod = package / "node_modules" / "zod" / "index.js"
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
        "PI_COMPACTION_TEST_PACKAGE": str(native_package),
        "AGENT_COMMS_AGENT_MODELS": "openrouter/z-ai/glm-5.3-flash",
    }
    api = native_package / "dist" / "index.js"
    _node_run(
        node,
        env,
        f"import {{ProjectTrustStore}} from {json.dumps(api.as_uri())};"
        f"new ProjectTrustStore({json.dumps(str(agent))}).set({json.dumps(str(project))},true);",
        project,
        root / "project-trust",
    )
    digest = _node_run(
        node,
        env,
        "import {declarationDigest,parseNativeConfig} from "
        f"{json.dumps((package / 'src/config.mjs').as_uri())};"
        f"console.log(declarationDigest(parseNativeConfig({json.dumps(document)}).servers[0]));",
        project,
        root / "mcp-declaration",
    )
    # Initial fixture setup uses the real package writer, never a Toad ledger.
    _node_run(
        node,
        env,
        "import {recordProjectDecision} from "
        f"{json.dumps((package / 'src/ledger-write.mjs').as_uri())};"
        f"await recordProjectDecision({{agentDir:{json.dumps(str(agent))},"
        f"projectRoot:{json.dumps(str(project))},declaration:{json.dumps(declaration)},decision:'approve'}});",
        project,
        root / "mcp-approval",
    )
    (agent / "settings.json").write_text(
        json.dumps(
            {
                "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}},
                "compaction": {"enabled": False},
            }
        )
    )
    return package, node, agent, project, digest, starts, calls, env


def _deny_via_simulated_user_pty(package, node, project, digest, env, artifact):
    """Acceptance-owned simulated user; never production auto-confirmation."""
    master, slave = pty.openpty()
    process = None
    output = bytearray()
    try:
        process = subprocess.Popen(
            [
                node,
                str(package / "bin/pi-mcp.mjs"),
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
    guard_requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            guard_requests.append({"path": self.path, "host": self.headers.get("Host")})
            if self.path == "/guard-positive":
                self.send_response(200)
                self.send_header("Content-Length", "0")
            elif self.path == "/guard-redirect":
                self.send_response(302)
                self.send_header("Location", f"https://127.0.0.1:{self.server.server_port}/guard-target")
                self.send_header("Content-Length", "0")
            else:
                errors.append(f"Unexpected guard request: {self.path}")
                self.send_response(500)
                self.send_header("Content-Length", "0")
            self.end_headers()

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
    try:
        yield requests, errors, origin
    finally:
        receipt_seen.set()
        release_final.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        (root / "model-requests.json").write_text(json.dumps(requests, indent=2))
        (root / "model-errors.json").write_text(json.dumps(errors))
        (root / "guard-http-requests.json").write_text(json.dumps(guard_requests, indent=2))


@asynccontextmanager
async def _observer(case, root):
    path = os.environ.get("AC_MCP_TOAD_ADAPTER")
    if not path:
        yield None
        return
    # The selected adapter owns its sibling test dependencies. Keep their
    # resolution in the same lifetime as its external open_observer contract.
    with pytest.MonkeyPatch.context() as imports:
        imports.syspath_prepend(str(Path(path).resolve().parent))
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
    assert binary == "pi" or (Path(binary).is_absolute() and os.access(binary, os.X_OK))
    native_package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    package, node, agent, project, digest, starts, calls, env = _prepare(
        tmp_path, native_package
    )
    receipt_seen, second_request, release_final = (threading.Event() for _ in range(3))
    if case != "revoke_midturn":
        release_final.set()
    attachment_settled = asyncio.Event()
    attachment_turn = None
    updates, permissions = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    task = proxy = owner = None
    (tmp_path / "launch.json").write_text(
        json.dumps(
            {
                "launcher": binary,
                "nativePackage": env["PI_COMPACTION_TEST_PACKAGE"],
                "package": str(package),
                "case": case,
            }
        )
    )
    async with _observer(case, tmp_path) as observer:
        with monkeypatch.context() as environment, _mock_model(
            tmp_path, agent, receipt_seen, second_request, release_final
        ) as (
            requests,
            errors,
            origin,
        ):
            env.update(
                OPENROUTER_API_KEY="offline-fixture-no-real-key",
                AGENT_COMMS_NATIVE_ORIGIN=origin,
            )
            if observer is not None:
                # The mounted observer already selected its isolated UI paths.
                for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
                    if key in os.environ:
                        env[key] = os.environ[key]
            environment.setattr(os, "environ", env)
            # The unchanged origin guard has its own retained SDK qualification.
            # These four cases exercise MCP/ACP under that committed policy.
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
                str(package),
            ]
            retirement = AsyncExitStack()
            try:
                owner = canonical_agent(
                    wire(tmp_path / "wire"),
                    agent_bin=binary,
                    agent_args=args,
                    runtime_enabled=True,
                    auto_wake=False,
                )
                retirement.push_async_callback(owner.shutdown)

                env.update(
                    AGENT_COMMS_ROOT=str(owner._comms.root),
                    AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=owner._private_nk_wire_root_id,
                    AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=env["PI_COMPACTION_TEST_PACKAGE"],
                    AGENT_COMMS_NATIVE_CONFIG_DIR=str(agent),
                )

                class Audit:
                    async def session_update(self, session_id, update):
                        row = {
                            "sessionId": session_id,
                            "update": update.model_dump(mode="json", by_alias=True, exclude_none=True),
                        }
                        updates.append(row)

                class Attachment:
                    async def session_update(self, session_id, update):
                        nonlocal attachment_turn
                        if observer:
                            await observer.session_update(session_id=session_id, update=update)
                        for fact in decode_updates(update.get("_meta")):
                            if isinstance(fact, McpClientReceiptUpdate):
                                # The original response gate belongs to this
                                # attachment, after any mounted observer has
                                # consumed the receipt. Passive owner logging
                                # cannot attest that the live view received it.
                                receipt_seen.set()
                            if isinstance(fact, TurnChangedUpdate):
                                if fact.state.busy:
                                    if attachment_turn is None:
                                        attachment_turn = fact.state
                                    else:
                                        assert fact.state.matches(attachment_turn.managed_id)
                                elif attachment_turn is not None:
                                    assert fact.state.matches(attachment_turn.managed_id)
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
                session = await owner.new_session(cwd=str(project), mcp_servers=[])
                session_id = session.session_id
                attached = canonical_agent(owner._comms, auto_wake=False)
                retirement.push_async_callback(attached.shutdown)
                attached.on_connect(Attachment())
                proxy = RuntimeProxy(attached, session_id, owner._runtime.path)
                retirement.push_async_callback(proxy.close)
                await proxy.subscribe()
                assert not attachment_settled.is_set()  # Initial idle replay is not this turn.
                if case == "no_controller":
                    context = owner._runtime.controller.set(None)
                    try:
                        task = asyncio.create_task(
                            owner.prompt(
                                session_id, [{"type": "text", "text": "Run fixture echo once."}]
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
                    assert (await Coordination.run_worker(partial(
                        owner.turns.turn_state, session_id
                    ))).busy
                    if case == "revoke_midturn":
                        await asyncio.to_thread(
                            _deny_via_simulated_user_pty,
                            package,
                            node,
                            project,
                            digest,
                            env,
                            tmp_path / "pty-deny.log",
                        )
                        assert (await Coordination.run_worker(partial(
                            owner.turns.turn_state, session_id
                        ))).busy  # Genuine mid-turn denial.
                    elif case == "disconnect":
                        if observer:
                            # Receipt of the RPC is not proof that the user saw
                            # an Ask. The UI adapter signals only after mounting.
                            await asyncio.wait_for(observer.permission_presented.wait(), 5)
                        await proxy.close()  # Actual controlling Unix-socket attachment loss.
                        if observer:
                            await observer.disconnected()
                    release.set()
                    if case == "revoke_midturn":
                        assert await asyncio.to_thread(second_request.wait, 12)
                        current = await Coordination.run_worker(partial(
                            owner.turns.turn_state, session_id
                        ))
                        assert current.busy
                        # The final model response is still held at localhost:
                        # connection retirement cannot be Pi turn shutdown.
                        for pid in map(int, starts.read_text().splitlines()):
                            with pytest.raises(ProcessLookupError):
                                os.kill(pid, 0)
                        (tmp_path / "midturn-retired.json").write_text(
                            json.dumps(
                                {
                                    "turnId": current.managed_id,
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
                active, receipts, settlements = None, [], []
                for row in updates:
                    assert row["sessionId"] == session_id
                    for fact in decode_updates(row["update"].get("_meta")):
                        if isinstance(fact, TurnChangedUpdate):
                            if fact.state.busy:
                                if active is None:
                                    active = fact.state
                                else:
                                    assert fact.state.matches(active.managed_id)
                            elif active is not None:
                                assert fact.state.matches(active.managed_id)
                                settlements.append(fact.state)
                                active = None
                        if isinstance(fact, McpClientReceiptUpdate):
                            assert active is not None and active.matches(fact.turn_id)
                            receipts.append(fact.receipt)
                assert active is None and len(settlements) == len(receipts) == 1, updates
                assert [FieldCodec.encode(server) for server in receipts[0].servers] == [
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
                try:
                    if task and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                finally:
                    try:
                        await retirement.aclose()
                    finally:
                        (tmp_path / "acp-updates.json").write_text(json.dumps(updates, indent=2))
                        if starts.exists():
                            for pid in map(int, starts.read_text().splitlines()):
                                with pytest.raises(ProcessLookupError):
                                    os.kill(pid, 0)
