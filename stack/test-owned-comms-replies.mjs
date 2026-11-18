import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { OwnerToolConnection, ToolNotStarted, ToolOutcomeUnknown } from "../src/agent_comms/owner_tool_socket.mjs";

const directory = await mkdtemp(join(tmpdir(), "owned-comms-reply-"));
const path = join(directory, "socket");
const large = { text: "x".repeat(2_000_000) };
const answers = [{ ok: true, result: large }, { ok: false, error: "original Core failure" }, { ok: true, result: null }];
let requests = 0;
const server = createServer(socket => socket.once("data", bytes => {
  const request = JSON.parse(bytes);
  assert.equal(request.call_id, `call-${requests}`);
  assert.equal(request.token, "f".repeat(64));
  assert.deepEqual(request.operation, { kind: "execute" });
  const reply = Buffer.from(JSON.stringify(answers[requests++]) + "\n");
  for (let offset = 0; offset < reply.length; offset += 8192) socket.write(reply.subarray(offset, offset + 8192));
  socket.end();
}));
try {
  await new Promise(resolve => server.listen(path, resolve));
  const owner = OwnerToolConnection.capture({ path, token: "f".repeat(64) });
  assert.deepEqual(await owner.execute("call-0", "comms_example", {}), large);
  await assert.rejects(owner.execute("call-1", "comms_example", {}), /original Core failure/);
  await assert.rejects(owner.execute("call-2", "comms_example", {}), ToolOutcomeUnknown);
  await assert.rejects(OwnerToolConnection.unavailable.execute("missing", "comms_example", {}), ToolNotStarted);
  assert.equal(requests, 3, "No failed or uncertain execution was retried");
} finally {
  await new Promise(resolve => server.close(resolve));
  await rm(directory, { recursive: true, force: true });
}
console.log("owned Core replies: whole large result, declared failure, unknown result, no replay");
