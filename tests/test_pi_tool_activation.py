"""The managed Pi catalog is registered once, but deferred tools belong to one input."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from agent_comms.comms import wire
from agent_comms.execution.coding_child import CodingChild, ToollessMode
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import Thread
from agent_comms.transport.native_tools import NativeToolDeclaration
from test_pi_adapter_reply_bound import HOOKS

DRIVER = """
import assert from "node:assert/strict";
import { register } from "node:module";
register(process.env.HOOKS_URL);
const { default: adapter } = await import(process.env.ADAPTER_URL);
const handlers = {}, tools = {};
let active = ["read", "bash", "edit", "write", "comms_discover", "comms_send"];
adapter({
  registerFlag() {}, getFlag: () => process.env.CATALOG,
  getActiveTools: () => active, setActiveTools: (names) => { active = names; },
  on: (event, handler) => { handlers[event] = handler; },
  registerTool: (tool) => { tools[tool.name] = tool; },
});
handlers.session_start();
assert.deepEqual(active, ["read", "bash", "edit", "write", "comms_discover"]);
const context = { ownerTools: { execute: async () => ({ tools: [{ name: "comms_send" }] }) } };
await tools.comms_discover.execute("discover", { kind: "search" }, undefined, undefined, context);
assert.deepEqual(active, ["read", "bash", "edit", "write", "comms_discover", "comms_send"]);
handlers.input({ type: "input", text: "next", source: "rpc", streamingBehavior: "followUp" });
assert.deepEqual(active, ["read", "bash", "edit", "write", "comms_discover"]);
assert.ok(tools.comms_send, "resetting activation must not delete the registered tool");
"""


def test_coding_child_does_not_launch_with_the_whole_catalog_active(tmp_path: Path) -> None:
    comms = wire(tmp_path / "bus")
    thread = comms.registry.declare(Thread("owner", frozenset(), Checkout(tmp_path)))
    for declaration in NativeToolDeclaration.members_with(NativeToolDeclaration):
        extension = declaration.native_extension(tmp_path)
        extension.parent.mkdir(parents=True, exist_ok=True)
        extension.touch()
    arguments = ToollessMode(CodingChild(comms=comms, thread=thread)).launch_arguments(tmp_path)
    assert "--tools" not in arguments


def test_discovered_tools_expire_at_the_next_input_without_leaving_the_registry(
        tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"launch_arguments": [], "tools": [
        {"name": "comms_discover", "label": "Discover", "description": "",
         "parameters": {}, "loads_tools": True},
        {"name": "comms_send", "label": "Send", "description": "",
         "parameters": {}, "loads_tools": False},
    ]}))
    hooks = tmp_path / "hooks.mjs"
    hooks.write_text(HOOKS)
    driver = tmp_path / "driver.mjs"
    driver.write_text(DRIVER)
    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_COMMS")}
    env.update(
        AGENT_COMMS_MANAGED="1",
        CATALOG=str(catalog),
        HOOKS_URL=hooks.as_uri(),
        ADAPTER_URL=(root / "extensions/pi-agent-comms/index.ts").as_uri(),
    )
    subprocess.run(["node", str(driver)], env=env, capture_output=True, text=True,
                   timeout=30, check=True)
