"""Drive the real managed Pi extension through its owner socket into a private bus."""

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_comms.comms import wire
from agent_comms.execution.channel_coding_tools import CodingToolOwner, CodingToolSocket
from agent_comms.execution.pi_events import ToolExecutionStart
from agent_comms.execution.provider_records import ReportedText, ToolCallContent
from agent_comms.execution.turn_goal_permission import InactiveGoalPermission, TurnGoalPermission
from agent_comms.messaging.messages import Message
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.pi_payloads import AssistantMessage, ReportedParts
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import Thread
from agent_comms.storage.storage_room import NoRoomNeeded
from agent_comms.transport.native_tools import NativeToolDeclaration
from agent_comms.transport.tool_denial import SelectedToolDenied
from test_pi_adapter_reply_bound import HOOKS


@dataclass(frozen=True, kw_only=True)
class Owner(CodingToolOwner):
    """Exercise real execution; other controls cover reservation and terminal publication."""

    @property
    def goal_permission(self):
        return TurnGoalPermission(InactiveGoalPermission())

    def verify(self, store, turn, snapshot):
        raise AssertionError("This control supplies only its preflight reservation")

    def claim_resource(self, store, turn, resource, slot):
        raise AssertionError("Comms calls claim no worktree file")

    async def allow(self, turn, round_number, call):
        return NoRoomNeeded()


DRIVER = """
import assert from 'node:assert/strict';
import {register} from 'node:module';
register(process.env.HOOKS_URL);
const {default: adapter} = await import(process.env.ADAPTER_URL);
const {default: codingTools} = await import(process.env.CODING_URL);
const {OwnerToolConnection} = await import(process.env.OWNER_URL);
const handlers = {}, tools = {};
const pi = {
  registerFlag() {}, getFlag: () => process.env.CATALOG,
  getActiveTools: () => [], setActiveTools() {},
  on: (event, handler) => {handlers[event] = handler;},
  registerTool: (tool) => {tools[tool.name] = tool;},
};
adapter(pi);
codingTools(pi);
handlers.session_start();
const ownerTools = OwnerToolConnection.capture(JSON.parse(process.env.ENDPOINT));
const context = {ownerTools};
await assert.rejects(tools.comms_model.execute('bad-model', {model:'missing/model'}, undefined, undefined,
  {ownerTools:{execute:()=>assert.fail('Local validation must precede Core execution')},
   modelRegistry:{find:()=>undefined}}), /Unknown model/);
const args = {to:'receiver', body:'one actual message'};
await assert.rejects(ownerTools.execute('send-once', 'comms_send', args), /unused owner authorization/);
assert.equal(await handlers.tool_call({toolCallId:'send-once', toolName:'comms_send', input:args}, context), undefined);
await assert.rejects(ownerTools.execute('send-once', 'comms_send', {...args, body:'changed'}), /differs/);
const result = await tools.comms_send.execute('send-once', args, undefined, undefined, context);
assert.equal(JSON.parse(result.content[0].text).id, result.details.id);
assert.ok(result.details.id);
await assert.rejects(ownerTools.execute('send-once', 'comms_send', args), /unused owner authorization/);
console.log(JSON.stringify(result.details));
"""


@pytest.mark.asyncio
async def test_managed_native_send_uses_its_existing_owner_once(tmp_path, tool_listener, undealt_gate):
    comms = wire(tmp_path / "bus")
    sender = comms.registry.declare(Thread("sender", frozenset(), Checkout(tmp_path)))
    comms.registry.declare(Thread("receiver", frozenset(), Checkout(tmp_path)))
    owner = Owner(comms=comms, thread=sender)
    socket = CodingToolSocket(tool_listener(tmp_path, "f" * 64), owner, undealt_gate, pid=0)
    # Only reserve_room uses the real turn. This control's owner supplies NoRoomNeeded.
    socket.turn = None
    args = {"to": "receiver", "body": "one actual message"}
    socket.announce(AssistantMessage(content=ReportedParts((
        ToolCallContent(id="send-once", name="comms_send", arguments=args),
    ))))
    socket.tool_started(ToolExecutionStart(
        tool_call_id=ReportedText("send-once"), tool_name=ReportedText("comms_send"), args=args))
    await socket.start()
    root = Path(__file__).resolve().parents[1]
    hooks = tmp_path / "hooks.mjs"
    hooks.write_text(HOOKS)
    driver = tmp_path / "driver.mjs"
    driver.write_text(DRIVER)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"launch_arguments": [], "tools": [
        {"name": name, "label": name, "description": "", "parameters": {}}
        for name in ("comms_send", "comms_model")
    ]}))
    binaries = tmp_path / "bin"
    binaries.mkdir()
    marker = tmp_path / "cli-was-started"
    cli = binaries / "agent-comms"
    cli.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
    cli.chmod(0o700)
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_COMMS")}
    env.update(
        PATH=f"{binaries}{os.pathsep}{env['PATH']}", AGENT_COMMS_MANAGED="1",
        AGENT_COMMS_THREAD="sender", CATALOG=str(catalog), HOOKS_URL=hooks.as_uri(),
        ADAPTER_URL=(root / "extensions/pi-agent-comms/index.ts").as_uri(),
        CODING_URL=(root / "src/agent_comms/channel_coding_tools.mjs").as_uri(),
        OWNER_URL=(root / "src/agent_comms/owner_tool_socket.mjs").as_uri(),
        ENDPOINT=json.dumps({"path": str(socket.listener.address), "token": socket.listener.token}),
    )
    child = await asyncio.create_subprocess_exec(
        "node", str(driver), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    socket.expected_pid = child.pid
    try:
        async with asyncio.timeout(30):
            stdout, stderr = await child.communicate()
        assert child.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        assert not marker.exists(), "Managed execution started a fresh CLI"
        inbox = await owner.execute_tool("comms_inbox", {"thread": "receiver"})
        messages = tuple(FieldCodec.decode(Message, row) for row in inbox["messages"])
        assert len(messages) == 1
        assert messages[0].sender == "sender"
        assert messages[0].body == args["body"]
        assert messages[0].id == result["id"]
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        await socket.close()


@pytest.mark.asyncio
async def test_interrupted_owner_execution_is_never_reopened():
    call = NativeToolDeclaration.decode("interrupted", "comms_send", {"to": "x", "body": "y"})
    call.announce()
    call.start()
    async def allowed():
        return None
    await call.authorize(allowed)
    entered = asyncio.Event()
    async def running():
        entered.set()
        await asyncio.Future()
    execution = asyncio.create_task(call.execute(running))
    await entered.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    with pytest.raises(SelectedToolDenied):
        await call.execute(running)
    with pytest.raises(SelectedToolDenied):
        await call.authorize(allowed)
