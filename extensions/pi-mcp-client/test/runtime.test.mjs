import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';
import { ProjectTrustStore } from '@earendil-works/pi-coding-agent';
import { McpRuntime } from '../src/runtime.mjs';
import { recordProjectDecision } from '../src/ledger-write.mjs';

const config = (declaration) => JSON.stringify({ version: 1, servers: [declaration] });

test('project stdio server spawns only after both gates, discovers and closes at session end',
  { skip: process.platform === 'win32' && 'Windows durable decision writes are disabled' }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-runtime-'));
  const agentDir = join(root, 'agent');
  const project = join(root, 'project');
  const configPath = join(project, '.pi', 'mcp.json');
  const wrapper = join(root, 'server.mjs');
  const spawned = join(root, 'spawned');
  const stopped = join(root, 'stopped');
  const fixtureUrl = pathToFileURL(join(import.meta.dirname, 'fixture-server.mjs')).href;
  await mkdir(agentDir);
  await mkdir(join(project, '.pi'), { recursive: true });
  await writeFile(wrapper, `import {writeFileSync} from 'node:fs';\n` +
    `writeFileSync(${JSON.stringify(spawned)}, 'started');\n` +
    `process.on('exit', () => writeFileSync(${JSON.stringify(stopped)}, 'closed'));\n` +
    `process.on('SIGTERM', () => process.exit(0));\n` +
    `await import(${JSON.stringify(fixtureUrl)});\n`);
  const declaration = { id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
    transport: { type: 'stdio', command: process.execPath, args: [wrapper], cwd: 'project' } };
  await writeFile(configPath, config(declaration));
  let trusted = false;
  const ctx = { cwd: project, isProjectTrusted: () => trusted };
  const options = { ctx, agentDir, configDirName: '.pi' };
  try {
    let runtime = new McpRuntime(options);
    await runtime.start();
    assert.deepEqual(runtime.snapshot(), []);
    assert.equal(existsSync(spawned), false);
    await runtime.stop();
    trusted = true;
    new ProjectTrustStore(agentDir).set(project, true);
    runtime = new McpRuntime(options);
    await runtime.start();
    assert.equal(runtime.snapshot()[0].status, 'trust_required');
    assert.equal(existsSync(spawned), false);
    await runtime.stop();
    await recordProjectDecision({ agentDir, projectRoot: project,
      declaration, decision: 'approve' }); // Test-owned explicit approval fixture.
    await writeFile(configPath, config({ ...declaration, transport: {
      ...declaration.transport, args: ['changed'] } }));
    runtime = new McpRuntime(options);
    await runtime.start();
    assert.equal(runtime.snapshot()[0].status, 'trust_required');
    assert.equal(existsSync(spawned), false);
    await runtime.stop();
    await writeFile(configPath, config(declaration));
    runtime = new McpRuntime(options);
    try {
      await runtime.start();
      assert.equal(runtime.snapshot()[0].status, 'ready');
      assert.deepEqual(runtime.snapshot()[0].tools, 2);
      assert.equal(runtime.snapshot()[0].resources, 1);
      assert.equal(runtime.snapshot()[0].prompts, 1);
      assert.equal(await readFile(spawned, 'utf8'), 'started');
      const ready = runtime.ready('fixture');
      assert.equal((await ready.client.callTool({ name: 'echo', arguments: { message: 'ok' } })).content[0].text, 'ok');
      await recordProjectDecision({ agentDir, projectRoot: project,
        declaration, decision: 'deny' });
      await runtime.refresh(ctx); // The package's /mcp-deny handler does this in finally.
      assert.equal(runtime.snapshot()[0].status, 'stale_restart_required');
      assert.equal(runtime.ready('fixture'), undefined);
      assert.equal(await readFile(stopped, 'utf8'), 'closed');
    } finally {
      await runtime.stop();
    }
    assert.equal(runtime.ready('fixture'), undefined);
    assert.equal(await readFile(stopped, 'utf8'), 'closed');
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
