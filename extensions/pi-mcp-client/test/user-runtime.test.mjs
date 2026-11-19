import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { ProjectTrustStore } from '@earendil-works/pi-coding-agent';
import { McpRuntime } from '../src/runtime.mjs';
import { registerReadyTools } from '../src/tools.mjs';
import { ToolRegistry } from './tool-registry.mjs';

for (const extraTools of [0, 123]) {
test(`user-profile ${extraTools + 2}-tool connection leaves project untrusted and each call needs confirmation`, async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-user-runtime-'));
  const agentDir = join(root, 'agent');
  const project = join(root, 'project');
  await mkdir(agentDir);
  await mkdir(join(project, '.pi'), { recursive: true });
  await writeFile(join(project, '.pi', 'mcp.json'), '{untrusted and invalid');
  await writeFile(join(agentDir, 'mcp.json'), JSON.stringify({ version: 1, servers: [{
    id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
    transport: { type: 'stdio', command: process.execPath,
      args: [join(import.meta.dirname, 'fixture-server.mjs'), '--extra-tools', String(extraTools)], cwd: 'agent' },
  }] }));
  let answer = false;
  const confirmations = [];
  const ctx = { cwd: project, mode: 'rpc', isProjectTrusted: () => false,
    ui: { async confirm(...args) { confirmations.push(args); return answer; } } };
  const runtime = new McpRuntime({ ctx, agentDir, configDirName: '.pi' });
  try {
    await runtime.start();
    assert.equal(runtime.snapshot()[0].status, 'ready');
    assert.equal(await runtime.preauthorized('fixture', ctx), false);
    const pi = new ToolRegistry();
    const registered = registerReadyTools(pi, runtime);
    assert.equal(registered.length, extraTools + 2);
    const name = registered.find((row) => row.remoteName === 'echo').name;
    if (extraTools > 0) {
      assert.deepEqual(pi.getActiveTools(), ['mcp_find_tools']);
      await pi.tool('mcp_find_tools').execute('find', { query: 'fixture/echo' });
      assert.ok(pi.getActiveTools().includes(name));
    }
    const echo = pi.tool(name);
    const signal = new AbortController().signal;
    await assert.rejects(echo.execute('denied', { message: 'ok' }, signal, undefined, ctx), /denied by user/);
    answer = true;
    const result = await echo.execute('confirmed', { message: 'ok' }, signal, undefined, ctx);
    assert.equal(result.content[0].text, 'ok');
    assert.equal(confirmations.length, 2);
    assert.equal(await runtime.preauthorized('fixture', ctx), false);
    assert.notEqual(new ProjectTrustStore(agentDir).get(project), true);
    assert.equal(existsSync(join(agentDir, 'mcp-trust.json')), false);
    assert.equal(await runtime.authorized('fixture', { ...ctx, cwd: agentDir }), false);
    assert.equal(runtime.ready('fixture'), undefined);
  } finally {
    await runtime.stop();
    await rm(root, { recursive: true, force: true });
  }
});
}
