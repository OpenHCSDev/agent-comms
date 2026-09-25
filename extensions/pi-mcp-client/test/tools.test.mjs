import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { McpRuntime } from '../src/runtime.mjs';
import { registerReadyTools } from '../src/tools.mjs';

const fixture = join(import.meta.dirname, 'fixture-server.mjs');
const declaration = { id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
  transport: { type: 'stdio', command: process.execPath, args: [fixture], cwd: 'project' } };
const config = (value) => JSON.stringify({ version: 1, servers: [value] });

test('Pi tools preserve discovered input schemas and require a human plus fresh declaration before one SDK call', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-tools-'));
  const agentDir = join(root, 'agent');
  const project = join(root, 'project');
  await mkdir(agentDir);
  await mkdir(project);
  const file = join(agentDir, 'mcp.json');
  await writeFile(file, config(declaration));
  const runtime = new McpRuntime({ ctx: { cwd: project, isProjectTrusted: () => true },
    agentDir, configDirName: '.pi' });
  const registered = [];
  const pi = { registerTool: (tool) => registered.push(tool), getAllTools: () => [] };
  let confirms = 0;
  const ctx = { mode: 'tui', cwd: project, isProjectTrusted: () => true,
    ui: { confirm: async () => { confirms++; return true; } } };
  try {
    await runtime.start();
    const names = registerReadyTools(pi, runtime);
    assert.equal(names.length, 2);
    assert.equal(registered.length, 2);
    assert.ok(names.every(({ name }) => name.startsWith('mcp_fixture_')));
    const echo = registered.find((tool) => tool.label.endsWith('/echo'));
    const signal = new AbortController().signal;
    assert.equal(echo.parameters.type, 'object');
    const response = await echo.execute('call-1', { message: 'hello' }, signal, undefined, ctx);
    assert.equal(response.content[0].text, 'hello');
    assert.equal(response.details.serverId, 'fixture');
    assert.equal(response.details.toolName, 'echo');
    const count = registered.find((tool) => tool.label.endsWith('/count'));
    const progress = [];
    const result = await count.execute('call-2', { n: 3 }, signal,
      (update) => progress.push(update.content[0].text), ctx);
    assert.equal(result.content[0].text, '3');
    assert.equal(progress.length, 3);
    assert.equal(confirms, 2);
    const headless = { ...ctx, mode: 'rpc' };
    await assert.rejects(echo.execute('call-3', { message: 'denied' }, signal, undefined, headless), /human controller/);
    assert.equal(confirms, 2);
    await writeFile(file, config({ ...declaration, transport: { ...declaration.transport, args: ['changed'] } }));
    await assert.rejects(echo.execute('call-4', { message: 'stale' }, signal, undefined, ctx), /no longer authorized/);
    const aborted = new AbortController(); aborted.abort();
    await assert.rejects(echo.execute('call-5', { message: 'cancelled' }, aborted.signal, undefined, ctx), /cancelled/);
  } finally {
    await runtime.stop();
    await rm(root, { recursive: true, force: true });
  }
});
