"""The Pi adapter answers a Core reply whole: a large one parses, an oversized one is refused."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ADAPTER = Path(__file__).resolve().parents[1] / "extensions/pi-agent-comms/index.ts"

# typebox is only the schema wrapper; the test drives the adapter's real execute path.
HOOKS = """
export async function resolve(specifier, context, next) {
  if (specifier === "typebox") return { url: "data:text/javascript,export const Type={Unsafe:(x)=>x};", shortCircuit: true };
  return next(specifier, context);
}
"""
DRIVER = """
import { register } from "node:module";
register(process.env.HOOKS_URL);
const { default: adapter } = await import(process.env.ADAPTER_URL);
const handlers = {}, tools = {};
adapter({
  registerFlag() {}, getFlag: () => process.env.CATALOG,
  on: (event, handler) => { handlers[event] = handler; },
  registerTool: (tool) => { tools[tool.name] = tool; },
});
handlers.session_start();
try {
  const result = await tools.comms_todo_list.execute("call", {}, undefined, undefined, {});
  console.log(JSON.stringify({ size: result.content[0].text.length }));
} catch (error) {
  console.log(JSON.stringify({ error: error.message }));
}
"""


def drive(tmp_path: Path, cli: str) -> dict[str, object]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "agent-comms"
    fake.write_text(f"#!/bin/sh\n{cli}\n")
    fake.chmod(0o755)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"launch_arguments": [], "tools": [
        {"name": "comms_todo_list", "label": "Read problems", "description": "", "parameters": {}}]}))
    hooks = tmp_path / "hooks.mjs"
    hooks.write_text(HOOKS)
    driver = tmp_path / "driver.mjs"
    driver.write_text(DRIVER)
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_COMMS")}
    env.update(PATH=f"{bin_dir}{os.pathsep}{env['PATH']}", CATALOG=str(catalog),
               HOOKS_URL=hooks.as_uri(), ADAPTER_URL=ADAPTER.as_uri())
    done = subprocess.run(["node", str(driver)], env=env, capture_output=True, text=True,
                          timeout=60, check=True)
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_reply_past_the_node_default_buffer_parses_whole(tmp_path: Path) -> None:
    # 2 MB: the board's measured size on 10-24 was 1,980,824 bytes, past Node's 1 MiB default.
    answer = drive(tmp_path, """python3 -c 'import json; print(json.dumps({"todos": ["x" * 2_000_000]}))'""")
    assert answer["size"] > 2_000_000


@pytest.mark.parametrize("cli, error", [
    ("""python3 -c 'import sys; sys.stdout.write("{" + "x" * (65 * 1024 * 1024))'""",
     "agent-comms replied with more than 67108864 bytes; the reply was refused whole"),
    ("""echo '{"error": "Unknown thread: nobody"}'; exit 1""", "Unknown thread: nobody"),
])
def test_an_unreadable_reply_names_its_failure(tmp_path: Path, cli: str, error: str) -> None:
    assert drive(tmp_path, cli) == {"error": error}
