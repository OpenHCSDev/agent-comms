"""Provider-free Pi extension UI relay controls over a real detached child pipe."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp.schema import PromptResponse

from agent_comms import acp as acp_module
from agent_comms import backend
from agent_comms.acp import CommsAgent
from agent_comms.operations import wire
from agent_comms.runtime import UNBOUND_CONTROLLER, RuntimeProxy

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable stub")


def stub(tmp_path: Path, body: str) -> str:
    path = tmp_path / "pi-rpc-stub"
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize("method,choice,answer", [
    ("confirm", {"confirmed": True}, {"confirmed": True}),
    ("confirm", None, {"cancelled": True}),
    ("confirm", {"value": "forged"}, {"confirmed": False}),
    ("select", {"value": "one"}, {"value": "one"}),
    ("select", {"value": "forged"}, {"cancelled": True}),
])
async def test_same_child_ui_reply_is_correlated_and_denied_without_controller(
    tmp_path, method, choice, answer
):
    marker = tmp_path / "received.json"
    program = stub(tmp_path, f'''
import json, sys
send = lambda row: print(json.dumps(row), flush=True)
state = json.loads(sys.stdin.readline())
send({{"id": state["id"], "type": "response", "command": "get_state", "success": True,
      "data": {{"nativeInputProofCapability": {json.dumps(backend.NATIVE_INPUT_CAPABILITY)},
                "sessionId": "fixture-pi-session"}}}})
prompt = json.loads(sys.stdin.readline())
send({{"id": prompt["id"], "type": "response", "command": "prompt", "success": True}})
send({{"type": "message_start", "message": {{"role": "user", "content": prompt["message"],
      "inputId": prompt["inputId"]}}}})
send({json.dumps({"type": "extension_ui_request", "id": "owned-ui-1", "method": method,
       "title": "Run one operation?", "message": "exact request", "options": ["one", "two"]})})
reply = json.loads(sys.stdin.readline())
with open({str(marker)!r}, "w") as output: json.dump(reply, output)
send({{"type": "message_end", "message": {{"role": "assistant", "stopReason": "stop"}}}})
send({{"type": "agent_settled"}})
''')
    requests = []

    async def controller(request):
        requests.append(request)
        return choice

    events = await asyncio.wait_for(collect_backend(program, tmp_path, controller if choice
        is not None else None), timeout=6)
    assert marker.exists()
    assert json.loads(marker.read_text()) == {
        "type": "extension_ui_response", "id": "owned-ui-1", **answer,
    }
    assert len(requests) == (0 if choice is None else 1)
    assert events[-1]["type"] == "done"


async def collect_backend(program, cwd, controller):
    return [event async for event in backend.stream_agent_events(program, [], "fixture", str(cwd),
        ui_request=controller)]


async def test_owner_permission_only_for_bound_live_subscriber_and_turn(tmp_path, monkeypatch):
    agent = CommsAgent(wire(tmp_path / "wire"))
    await agent.new_session(cwd=str(tmp_path / "project"), mcp_servers=[])
    session_id = "project"
    turn = "turn-1"
    agent._active_turns[session_id] = turn
    request = {"id": "ui-1", "method": "confirm", "title": "Confirm", "message": "One action"}
    class DirectController:
        async def request_permission(self, **kwargs):
            assert kwargs["session_id"] == session_id
            return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
    controller = DirectController()
    agent._client = controller
    assert (await agent._extension_ui_permission(session_id, turn, controller, request)) == {
        "confirmed": True,
    }
    assert (await agent._extension_ui_permission(
        session_id, "wrong-turn", controller, request
    )) is None
    assert await agent._extension_ui_permission(session_id, turn, None, request) is None
    assert agent._runtime.controller.get() is UNBOUND_CONTROLLER
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}

    controller.request_permission = delayed
    in_flight = asyncio.create_task(agent._extension_ui_permission(
        session_id, turn, controller, request
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    agent._active_turns[session_id] = "successor-turn"
    release.set()
    assert await asyncio.wait_for(in_flight, timeout=1) is None
    agent._active_turns[session_id] = turn
    monkeypatch.setattr(acp_module, "ACP_PERMISSION_TIMEOUT_SECONDS", 0.05)

    async def unresponsive(**kwargs):
        await asyncio.Event().wait()

    controller.request_permission = unresponsive
    assert await asyncio.wait_for(agent._extension_ui_permission(
        session_id, turn, controller, request
    ), timeout=1) is None
    await agent.shutdown()


async def test_private_subscriber_token_routes_only_active_prompt_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "openrouter/z-ai/glm-5.3-flash")
    owner = CommsAgent(wire(tmp_path / "wire"), runtime_enabled=True, auto_wake=False)
    await owner.new_session(cwd=str(tmp_path / "project"), mcp_servers=[])
    session_id = "project"
    calls = [[], []]
    proxies = []
    try:
        for index in range(2):
            async def update(**kwargs):
                pass

            async def answer(*, position=index, **kwargs):
                calls[position].append(kwargs)
                return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
            instance = SimpleNamespace(session_update=update, request_permission=answer)
            fake = SimpleNamespace(_comms=owner._comms, _transcript_snapshots=False,
                _transcript_diffs=False, _client=instance)
            proxy = RuntimeProxy(fake, session_id, owner._runtime.path)
            await proxy.subscribe()
            proxies.append(proxy)
        assert proxies[0]._controller_token != proxies[1]._controller_token
        assert "controllerToken" not in owner._session_metadata(session_id)["agentComms"]
        request = {"id": "only-one-child", "method": "confirm",
                   "title": "Approve once?", "message": "Exactly this request"}

        async def fake_prompt(session_id, prompt, **kwargs):
            controller = owner._runtime.controller.get()
            owner._active_turns[session_id] = "private-turn"
            try:
                answer = await owner._extension_ui_permission(
                    session_id, "private-turn", controller, request)
            finally:
                owner._active_turns.pop(session_id, None)
            return PromptResponse(stop_reason="end_turn", field_meta={"answer": answer})

        monkeypatch.setattr(owner, "prompt", fake_prompt)
        result = await asyncio.wait_for(proxies[0].request("prompt", prompt=[]), timeout=4)
        assert result["_meta"]["answer"] == {"confirmed": True}
        assert len(calls[0]) == 1 and not calls[1]
        assert calls[0][0]["session_id"] == session_id
        assert calls[0][0]["options"][0]["kind"] == "allow_once"
        # No subscriber may borrow another attachment's controller token.
        saved = proxies[1]._controller_token
        proxies[1]._controller_token = "0" * 64
        result = await asyncio.wait_for(proxies[1].request("prompt", prompt=[]), timeout=4)
        assert result["_meta"]["answer"] is None
        assert not calls[1]
        proxies[1]._controller_token = saved
        # A controller can disappear after presentation but before answering.
        # The detached owner must deny and must not transfer the pending
        # prompt to the still-connected passive second attachment.
        entered = asyncio.Event()
        unresolved = asyncio.Event()

        async def blocked_answer(**kwargs):
            entered.set()
            await unresolved.wait()
            return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}

        proxies[0].agent._client.request_permission = blocked_answer
        pending = asyncio.create_task(proxies[0].request("prompt", prompt=[]))
        await asyncio.wait_for(entered.wait(), timeout=4)
        await proxies[0].close()
        disconnected = await asyncio.wait_for(pending, timeout=4)
        assert disconnected["_meta"]["answer"] is None
        assert not calls[1]
    finally:
        for proxy in proxies:
            await proxy.close()
        await owner.shutdown()


@pytest.mark.parametrize("has_controller", [True, False])
async def test_detached_acp_turn_uses_real_package_sdk_without_model_or_provider(
    tmp_path, monkeypatch, has_controller
):
    """Composed fake-Pi RPC, real package SDK, real stdio MCP, real ACP owner.

    The fake Pi emits protocol events and directly invokes package-owned tools;
    this is not evidence of a real model call or real Pi tool dispatcher.
    """
    package = Path(__file__).resolve().parents[1] / "extensions" / "pi-mcp-client"
    node = subprocess.run(
        ["which", "node"], capture_output=True, text=True, check=True
    ).stdout.strip()
    agent_dir = tmp_path / "agent"
    project = tmp_path / "project"
    agent_dir.mkdir()
    project.mkdir()
    starts = tmp_path / "server-starts"
    server_fixture = (package / "test" / "fixture-server.mjs").as_uri()
    wrapper = tmp_path / "server-wrapper.mjs"
    wrapper.write_text(f'''
import {{appendFileSync}} from 'node:fs';
appendFileSync({json.dumps(str(starts))}, String(process.pid) + '\\n');
await import({json.dumps(server_fixture)});
''')
    config = {"version": 1, "servers": [{
        "id": "fixture", "enabled": True, "instructionsPolicy": "status-only",
        "transport": {"type": "stdio", "command": node,
                      "args": [str(wrapper)], "cwd": "project"},
    }]}
    (agent_dir / "mcp.json").write_text(json.dumps(config))
    isolated = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
                "PI_CODING_AGENT_DIR": str(agent_dir), "CI": "true", "NO_COLOR": "1",
                "AGENT_COMMS_AGENT_MODELS": "openrouter/z-ai/glm-5.3-flash"}
    trust = package / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "index.js"
    saved = subprocess.run([node, "--input-type=module", "-e", f'''
import {{ProjectTrustStore}} from {json.dumps(trust.as_uri())};
new ProjectTrustStore({json.dumps(str(agent_dir))}).set({json.dumps(str(project))}, true);
'''], cwd=project, env=isolated, capture_output=True, text=True, timeout=5)
    assert saved.returncode == 0, saved.stderr
    script = tmp_path / "pi-mcp-rpc-stub"
    script.write_text(f'''#!{node}
import {{createInterface}} from 'node:readline';
import {{McpRuntime}} from {json.dumps((package / "src" / "runtime.mjs").as_uri())};
import {{registerReadyTools}} from {json.dumps((package / "src" / "tools.mjs").as_uri())};
const send = (row) => process.stdout.write(JSON.stringify(row) + '\\n');
const ctx = {{cwd:process.cwd(), isProjectTrusted:()=>true, mode:'rpc', ui:{{
  confirm(title,message,opts) {{
    const id = 'owned-confirm';
    send({{type:'extension_ui_request',id,method:'confirm',title,message,timeout:opts.timeout}});
    return new Promise(resolve => {{ pending.set(id,resolve); }});
  }},
}}}};
const pending = new Map();
const runtime = new McpRuntime({{
  ctx,agentDir:process.env.PI_CODING_AGENT_DIR,configDirName:'.pi'
}});
await runtime.start();
const tools = [];
registerReadyTools({{getAllTools:()=>[],registerTool:(tool)=>tools.push(tool)}},runtime);
const echo = tools.find(tool => tool.label.endsWith('/echo'));
if (!echo) throw new Error('fixture tool was not discovered');
const input = createInterface({{input:process.stdin,crlfDelay:Infinity}});
let settled = false;
input.on('line', async line => {{
  const row=JSON.parse(line);
  if (row.type==='get_state') {{
    send({{id:row.id,type:'response',command:'get_state',success:true,data:{{
      nativeInputProofCapability:{json.dumps(backend.NATIVE_INPUT_CAPABILITY)},
      sessionId:'fixture-session',sessionFile:{json.dumps(str(tmp_path / "fixture-session.jsonl"))}
    }}}});
  }} else if (row.type==='prompt') {{
    send({{type:'response',id:row.id,command:'prompt',success:true}});
    send({{type:'message_start',message:{{role:'user',content:row.message,inputId:row.inputId}}}});
    send({{type:'tool_execution_start',toolCallId:'call-1',toolName:echo.name,
          args:{{message:'from-sdk'}}}});
    try {{
      const result=await echo.execute('call-1',{{message:'from-sdk'}},new AbortController().signal,
        undefined,ctx);
      send({{type:'tool_execution_end',toolCallId:'call-1',toolName:echo.name,result}});
    }} catch {{
      send({{type:'tool_execution_end',toolCallId:'call-1',toolName:echo.name,
            result:{{content:[{{type:'text',text:'denied'}}]}},isError:true}});
    }}
    send({{type:'message_end',message:{{role:'assistant',stopReason:'stop'}}}});
    send({{type:'agent_settled'}});
    settled=true;
  }} else if (row.type==='extension_ui_response') {{
    pending.get(row.id)?.(row.confirmed===true);
    pending.delete(row.id);
  }} else if (row.type==='get_session_stats') {{
    send({{type:'response',id:row.id,command:'get_session_stats',success:true,
      data:{{sessionId:'fixture-session',contextUsage:{{tokens:1}}}}}});
    if (settled) {{ await runtime.stop(); process.exit(0); }}
  }}
}});
''')
    script.chmod(0o755)
    monkeypatch.setattr(backend.os, "environ", isolated)
    owner = CommsAgent(wire(tmp_path / "wire"), agent_bin=str(script),
                       agent_args=[], auto_wake=False)
    updates = []
    approvals = []

    class Controller:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

        async def request_permission(self, **kwargs):
            approvals.append(kwargs)
            return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}

    owner.on_connect(Controller())  # Also observe a no-controller autonomous turn.
    await owner.new_session(cwd=str(project), mcp_servers=[])
    try:
        for _ in range(2):
            # A detached owner can have passive observers without any active
            # controller. A bound None must not fall back to owner._client.
            context = owner._runtime.controller.set(None) if not has_controller else None
            try:
                result = await asyncio.wait_for(owner.prompt("project", [
                    {"type": "text", "text": "run package fixture"}]), timeout=15)
            finally:
                if context is not None:
                    owner._runtime.controller.reset(context)
            assert result.stop_reason == "end_turn"
        assert len(approvals) == (2 if has_controller else 0)
        assert len(starts.read_text().splitlines()) == 2
        for pid in map(int, starts.read_text().splitlines()):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        rendered = [update.model_dump(by_alias=True, exclude_none=True)
                    for update in updates]
        if has_controller:
            assert sum('from-sdk' in json.dumps(update) for update in rendered) >= 2
        else:
            assert sum('denied' in json.dumps(update) for update in rendered) >= 2
    finally:
        await owner.shutdown()
