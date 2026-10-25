#!/usr/bin/env node
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { requestOwnerTool } from "../src/agent_comms/owner_tool_socket.mjs";

const root = await mkdtemp(join(tmpdir(), "agent-comms-owner-tool-"));
const path = join(root, "socket");
const server = createServer((socket) => {
  socket.once("data", () => {
    setTimeout(() => socket.end('{"ok":true}\n'), 12_250);
  });
});
try {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(path, resolve);
  });
  const begun = performance.now();
  assert.deepEqual(
    await requestOwnerTool(path, Buffer.from("request\n"), 4096),
    { ok: true },
  );
  assert.ok(performance.now() - begun >= 12_000);
} finally {
  await new Promise((resolve) => server.close(resolve));
  await rm(root, { recursive: true, force: true });
}
console.log("owner tool waited for delayed decision");
