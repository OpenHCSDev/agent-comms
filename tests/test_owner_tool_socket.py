"""A failed owner tool request says whether the owner could have claimed the call."""

import json
import subprocess
from pathlib import Path

import agent_comms

SOCKET_MODULE = Path(agent_comms.__file__).with_name("owner_tool_socket.mjs")


def test_only_a_written_request_leaves_its_claim_unknown(tmp_path):
    code = r"""
import {createServer} from 'node:net';
import {pathToFileURL} from 'node:url';
const [module] = process.argv.slice(1);
const {requestOwnerTool} = await import(pathToFileURL(module));
const outcome = (path) => requestOwnerTool(path, Buffer.from('{}\n'), 16384, 'UNKNOWN')
  .then(() => 'answered', (error) => error.message);
const closing = createServer((socket) => socket.once('data', () => socket.end()));
// Relative paths keep the endpoint below the AF_UNIX path-length limit.
const path = 's';
await new Promise((resolve) => closing.listen(path, resolve));
const results = {missing: await outcome('missing'), unanswered: await outcome(path)};
closing.close();
console.log(JSON.stringify(results));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", code, str(SOCKET_MODULE)],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "missing": "Tool owner is unavailable; no tool was started.",
        "unanswered": "UNKNOWN",
    }
