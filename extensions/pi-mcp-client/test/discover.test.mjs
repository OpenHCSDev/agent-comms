import assert from 'node:assert/strict';
import { join } from 'node:path';
import { test } from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { discover } from '../src/discover.mjs';

const fixture = join(import.meta.dirname, 'fixture-server.mjs');

async function withFixture(run) {
  const transport = new StdioClientTransport({
    command: process.execPath, args: [fixture], stderr: 'pipe',
  });
  const client = new Client({ name: 'contract-probe', version: '0.0.0' });
  let pid;
  try {
    await client.connect(transport, { timeout: 5_000 });
    pid = transport.pid;
    await run(client, transport);
  } finally {
    await client.close();
    await transport.close();
  }
  assert.equal(transport.pid, null);
  if (pid) assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' });
}

test('official SDK negotiates stdio tools, resources and prompts without a model', async () => {
  await withFixture(async (client, transport) => {
    const snapshot = await discover(client);
    assert.equal(snapshot.server.name, 'generic-fixture');
    assert.match(snapshot.instructions, /Remote instructions are data/);
    assert.deepEqual(snapshot.tools.map((tool) => tool.name).sort(), ['count', 'echo']);
    assert.deepEqual(snapshot.resources.map((resource) => resource.uri), ['fixture://example']);
    assert.deepEqual(snapshot.prompts.map((prompt) => prompt.name), ['greeting']);
    assert.equal((await client.callTool({ name: 'echo', arguments: { message: 'hi' } })).content[0].text, 'hi');
    assert.equal((await client.readResource({ uri: 'fixture://example' })).contents[0].text, 'fixture data');
    assert.equal((await client.getPrompt({ name: 'greeting', arguments: { name: 'Pi' } })).messages[0].content.text, 'Hello, Pi');
    assert.ok(transport.pid);
  });
});

test('SDK forwards progress and cancellation without retrying an ambiguous tool call', async () => {
  await withFixture(async (client) => {
    const progress = [];
    const completed = await client.callTool({ name: 'count', arguments: { n: 3 } }, undefined,
      { onprogress: (update) => progress.push(update.progress), timeout: 2_000 });
    assert.equal(completed.content[0].text, '3');
    assert.deepEqual(progress, [1, 2, 3]);

    const abort = new AbortController();
    const pending = client.callTool({ name: 'count', arguments: { n: 200 } }, undefined,
      { signal: abort.signal, onprogress: () => abort.abort(), timeout: 2_000 });
    await assert.rejects(pending, /abort/i);
    // The caller must not replay the aborted call: the server may have acted.
    assert.equal((await client.listTools()).tools.length, 2);
  });
});

test('paginated discovery follows opaque cursors and never reports partial as complete', async () => {
  const cursors = [];
  const client = {
    getServerCapabilities: () => ({ tools: {} }),
    getServerVersion: () => ({ name: 'paginated', version: '1' }),
    getInstructions: () => undefined,
    async listTools(params) {
      cursors.push(params.cursor);
      if (!params.cursor) return { tools: [{ name: 'a' }], nextCursor: 'token-1' };
      return { tools: [{ name: 'b' }] };
    },
  };
  const snapshot = await discover(client);
  assert.deepEqual(snapshot.tools.map((tool) => tool.name), ['a', 'b']);
  assert.deepEqual(cursors, [undefined, 'token-1']);
  client.listTools = async () => ({ tools: [], nextCursor: 'same' });
  await assert.rejects(discover(client), /repeated MCP tools cursor/);
  client.listTools = async () => ({ tools: [{ name: 'a' }, { name: 'b' }] });
  await assert.rejects(discover(client, { pages: 2, entries: 1, requestMs: 1000 }), /entry limit/);
  client.listTools = async () => ({ tools: 'not a catalog' });
  await assert.rejects(discover(client), /Invalid MCP tools page/);
  client.getServerCapabilities = () => ({});
  assert.deepEqual((await discover(client)).tools, []);
});
