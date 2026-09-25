import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { McpRuntime } from '../src/runtime.mjs';
import { prepareStdioParameters } from '../src/launch-spec.mjs';
import { recordProjectDecision } from '../src/ledger-write.mjs';

const config = (server) => JSON.stringify({ version: 1, servers: [server] });

test('changed/revoked approval during async launch preparation never starts a stale server', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-launch-race-'));
  const agentDir = join(root, 'agent');
  const project = join(root, 'project');
  const source = join(project, '.pi', 'mcp.json');
  const marker = join(root, 'unsafe-start');
  await mkdir(agentDir); await mkdir(join(project, '.pi'), { recursive: true });
  const declaration = { id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
    transport: { type: 'stdio', command: process.execPath, args: ['-e',
      `require('fs').writeFileSync(${JSON.stringify(marker)},'started')`], cwd: 'project' } };
  const ctx = { cwd: project, isProjectTrusted: () => true };
  const options = { agentDir, configDirName: '.pi', ctx };
  try {
    for (const mutation of ['changed', 'denied']) {
      await writeFile(source, config(declaration));
      await recordProjectDecision({ agentDir, projectRoot: project,
        declaration, decision: 'approve' });
      let entered;
      const preparing = new Promise((resolve) => { entered = resolve; });
      let release;
      const pending = new Promise((resolve) => { release = resolve; });
      const runtime = new McpRuntime(options, { prepare: async (entry, context) => {
        const parameters = await prepareStdioParameters(entry, context);
        entered();
        await pending;
        return parameters;
      } });
      const started = runtime.start();
      try {
        await preparing;
        if (mutation === 'changed') {
          await writeFile(source, config({ ...declaration, transport: {
            ...declaration.transport, args: ['different-command'] } }));
        } else {
          await recordProjectDecision({ agentDir, projectRoot: project,
            declaration, decision: 'deny' });
        }
        release();
        await started;
        assert.notEqual(runtime.snapshot()[0].status, 'ready');
        assert.equal(existsSync(marker), false);
      } finally {
        release();
        await runtime.stop();
      }
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('a user-scope relative command never runs from an untrusted project', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-user-untrusted-'));
  const agentDir = join(root, 'agent');
  const project = join(root, 'project');
  const marker = join(root, 'unsafe-start');
  await mkdir(agentDir); await mkdir(project);
  const script = join(project, 'project-controlled.mjs');
  await writeFile(script, `import {writeFileSync} from 'node:fs';\nwriteFileSync(${JSON.stringify(marker)},'unsafe');`);
  const declaration = { id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
    transport: { type: 'stdio', command: process.execPath,
      args: ['./project-controlled.mjs'], cwd: 'project' } };
  await writeFile(join(agentDir, 'mcp.json'), config(declaration));
  const ctx = { cwd: project, isProjectTrusted: () => false };
  const runtime = new McpRuntime({ agentDir, configDirName: '.pi', ctx });
  try {
    await runtime.start();
    assert.equal(runtime.snapshot()[0].status, 'trust_required');
    assert.equal(existsSync(marker), false);
  } finally {
    await runtime.stop();
    await rm(root, { recursive: true, force: true });
  }
});
