import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { McpRuntime } from '../src/runtime.mjs';
import { registerResourceTools } from '../src/resources.mjs';

const fixture = join(import.meta.dirname, 'fixture-server.mjs');
const declaration = { id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
  transport: { type: 'stdio', command: process.execPath, args: [fixture], cwd: 'project' } };
const config = (value) => JSON.stringify({ version: 1, servers: [value] });

test('MCP resources and prompts are discoverable and usable as Pi tools with call authorization', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-resources-'));
  const agentDir = join(root, 'agent');
  const project = join(root, 'project');
  await mkdir(agentDir);
  await mkdir(project);
  const path = join(agentDir, 'mcp.json');
  await writeFile(path, config(declaration));
  const runtime = new McpRuntime({ ctx: { cwd: project, isProjectTrusted: () => false },
    agentDir, configDirName: '.pi' });
  const registered = new Map();
  const pi = { getAllTools: () => [], registerTool: (tool) => registered.set(tool.name, tool) };
  let approvals = 0;
  const ctx = { mode: 'tui', cwd: project, isProjectTrusted: () => false,
    ui: { confirm: async () => { approvals++; return true; } } };
  try {
    await runtime.start();
    registerResourceTools(pi, runtime);
    assert.deepEqual([...registered.keys()], ['mcp_catalog', 'mcp_read_resource', 'mcp_get_prompt']);
    const signal = new AbortController().signal;
    const catalog = await registered.get('mcp_catalog').execute('', {}, signal, undefined, ctx);
    assert.match(catalog.content[0].text, /fixture:\/\/example/);
    assert.match(catalog.content[0].text, /greeting/);
    assert.equal(approvals, 0);
    const resource = await registered.get('mcp_read_resource').execute('',
      { serverId: 'fixture', uri: 'fixture://example' }, signal, undefined, ctx);
    assert.match(resource.content[0].text, /fixture data/);
    assert.equal(resource.details.resourceUri, 'fixture://example');
    const prompt = await registered.get('mcp_get_prompt').execute('',
      { serverId: 'fixture', name: 'greeting', arguments: { name: 'Pi' } }, signal, undefined, ctx);
    assert.match(prompt.content[0].text, /Hello, Pi/);
    assert.equal(approvals, 2);
    await assert.rejects(registered.get('mcp_read_resource').execute('',
      { serverId: 'fixture', uri: 'fixture://unknown' }, signal, undefined, ctx), /not discovered/);
    await assert.rejects(registered.get('mcp_get_prompt').execute('',
      { serverId: 'fixture', name: 'greeting' }, signal, undefined, { ...ctx, mode: 'rpc' }), /human controller/);
    assert.equal(approvals, 2);
    await writeFile(path, config({ ...declaration, transport: { ...declaration.transport, args: ['changed'] } }));
    await assert.rejects(registered.get('mcp_read_resource').execute('',
      { serverId: 'fixture', uri: 'fixture://example' }, signal, undefined, ctx), /no longer authorized/);
  } finally {
    await runtime.stop();
    await rm(root, { recursive: true, force: true });
  }
});
