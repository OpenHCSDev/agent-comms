import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { ProjectTrustStore } from '@earendil-works/pi-coding-agent';
import { decideCallGrant } from '../src/commands.mjs';
import { McpRuntime } from '../src/runtime.mjs';
import { registerReadyTools } from '../src/tools.mjs';

const fixture = join(import.meta.dirname, 'fixture-server.mjs');
const declaration = { id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
  transport: { type: 'stdio', command: process.execPath, args: [fixture], cwd: 'project' } };
const config = (value) => JSON.stringify({ version: 1, servers: [value] });

test('separate TUI call grant enables and revokes headless calls only for the exact declaration',
  { skip: process.platform === 'win32' && 'Windows durable decision writes are disabled' }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-grant-'));
  const agentDir = join(root, 'agent');
  const project = join(root, 'project');
  await mkdir(agentDir); await mkdir(project);
  const path = join(agentDir, 'mcp.json');
  await writeFile(path, config(declaration));
  new ProjectTrustStore(agentDir).set(project, true);
  const runtime = new McpRuntime({ ctx: { cwd: project, isProjectTrusted: () => true },
    agentDir, configDirName: '.pi' });
  const registered = [];
  const pi = { registerTool: (tool) => registered.push(tool), getAllTools: () => [] };
  let confirmations = 0;
  let prompt = '';
  const tui = { mode: 'tui', cwd: project, isProjectTrusted: () => true,
    ui: { confirm: async (title, body) => { confirmations++; prompt = title + body; return true; } } };
  const headless = { ...tui, mode: 'rpc', ui: { confirm: async (_title, _body, opts) => {
    assert.equal(opts.timeout, 15_000);
    confirmations++;
    return false; // No eligible active controller.
  } } };
  const controlled = { ...tui, mode: 'rpc' };
  const options = { agentDir, configDirName: '.pi', id: 'fixture' };
  try {
    await runtime.start(); registerReadyTools(pi, runtime);
    const echo = registered.find((tool) => tool.label.endsWith('/echo'));
    const args = { message: 'headless-ok' };
    const signal = new AbortController().signal;
    await assert.rejects(echo.execute('', args, signal, undefined, headless), /denied by user/);
    assert.equal(confirmations, 1);
    const approvedOnce = await echo.execute('', args, signal, undefined, controlled);
    assert.equal(approvedOnce.content[0].text, 'headless-ok');
    assert.equal(confirmations, 2);
    await assert.rejects(decideCallGrant(headless, { ...options, decision: 'allow' }), /local interactive TUI/);
    assert.equal(await decideCallGrant(tui, { ...options, decision: 'allow' }), true);
    assert.match(prompt, /ALL MCP tools, resource reads and prompt retrieval/);
    assert.equal(await runtime.preauthorized('fixture', headless), true);
    const response = await echo.execute('', args, signal, undefined, headless);
    assert.equal(response.content[0].text, 'headless-ok');
    assert.equal(confirmations, 3); // The grant required a TUI, not an RPC approval.
    assert.equal(await decideCallGrant(tui, { ...options, decision: 'ask' }), true);
    assert.equal(await runtime.preauthorized('fixture', headless), false);
    await assert.rejects(echo.execute('', args, signal, undefined, headless), /denied by user/);
    await writeFile(path, config({ ...declaration, transport: {
      ...declaration.transport, args: ['changed'] } }));
    await assert.rejects(echo.execute('', args, signal, undefined, headless), /no longer authorized|approved server/);
    assert.equal(runtime.ready('fixture'), undefined);
    await writeFile(path, config(declaration));
    assert.equal(runtime.ready('fixture'), undefined); // No silent reconnect after stale mutation.
  } finally {
    await runtime.stop();
    await rm(root, { recursive: true, force: true });
  }
});
