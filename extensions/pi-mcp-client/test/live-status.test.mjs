import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ProjectTrustStore } from '@earendil-works/pi-coding-agent';
import piMcpExtension from '../index.mjs';
import { liveStatusReceipt } from '../src/live-status.mjs';

const INPUT = 'a'.repeat(32);

test('typed package receipt is redacted, input-bound and non-authorizing', async () => {
  const calls = [];
  const runtime = {
    isRunning: () => true,
    snapshot: () => [
      { id: 'one', scope: 'user', status: 'ready', server: 'private host',
        stderrBytes: 201, tools: 2, resources: 1, prompts: 0 },
      { id: 'two', scope: 'project', status: 'denied', tools: 0, resources: 0, prompts: 0 },
    ],
    authorized: async (id) => { calls.push(['authorized', id]); return true; },
    preauthorized: async (id) => { calls.push(['preauthorized', id]); return false; },
    ready: () => ({}),
  };
  const result = await liveStatusReceipt(runtime, { cwd: '/private/project' }, INPUT);
  assert.deepEqual(result, {
    version: 1, source: 'pi-mcp-client', inputId: INPUT, state: 'running',
    lifetime: 'turn', servers: [
      { id: 'one', scope: 'user', state: 'ready', calls: 'confirm',
        tools: 2, resources: 1, prompts: 0 },
      { id: 'two', scope: 'project', state: 'denied', calls: 'unavailable',
        tools: 0, resources: 0, prompts: 0 },
    ],
  });
  assert.deepEqual(calls, [['authorized', 'one'], ['preauthorized', 'one']]);
  assert.doesNotMatch(JSON.stringify(result), /private|stderrBytes|host|transport|digest/);
  assert.equal(await liveStatusReceipt(runtime, {}, 'forged'), undefined);
});

test('Pi extension emits a live receipt only after native user input in RPC mode', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-mcp-live-'));
  const previous = process.env.PI_CODING_AGENT_DIR;
  const agentDir = join(root, 'agent');
  const project = join(root, 'project');
  await mkdir(agentDir);
  await mkdir(project);
  await writeFile(join(agentDir, 'mcp.json'), '{"version":1,"servers":[]}');
  new ProjectTrustStore(agentDir).set(project, true);
  process.env.PI_CODING_AGENT_DIR = agentDir;
  const handlers = new Map();
  const emitted = [];
  let timer;
  let complete;
  const status = new Promise((resolve) => { complete = resolve; });
  const context = { cwd: project, mode: 'rpc', isProjectTrusted: () => true,
    ui: { setStatus(key, value) { emitted.push({ key, value }); complete(); } } };
  const pi = { on(name, handler) { handlers.set(name, handler); },
    registerCommand() {}, getAllTools: () => [], registerTool() {} };
  try {
    piMcpExtension(pi);
    await handlers.get('session_start')(undefined, context);
    handlers.get('message_start')({ message: { role: 'user', inputId: 'invalid' } }, context);
    handlers.get('message_start')({ message: { role: 'assistant', inputId: INPUT } }, context);
    assert.equal(emitted.length, 0);
    handlers.get('message_start')({ message: { role: 'user', inputId: INPUT } }, context);
    await Promise.race([status, new Promise((_, reject) => { timer = setTimeout(() =>
      reject(new Error('live receipt timeout')), 1000); })]);
    assert.equal(emitted.length, 1);
    assert.equal(emitted[0].key, 'pi-mcp/live-v1');
    assert.deepEqual(JSON.parse(emitted[0].value), {
      version: 1, source: 'pi-mcp-client', inputId: INPUT, state: 'running',
      lifetime: 'turn', servers: [],
    });
  } finally {
    clearTimeout(timer);
    await handlers.get('session_shutdown')?.();
    if (previous === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = previous;
    await rm(root, { recursive: true, force: true });
  }
});

test('revocation while producing status never reports a ready connection', async () => {
  const runtime = {
    isRunning: () => true,
    snapshot: () => [{ id: 'one', scope: 'project', status: 'ready', tools: 1 }],
    authorized: async () => true,
    preauthorized: async () => false,
    ready: () => undefined,
  };
  assert.deepEqual((await liveStatusReceipt(runtime, {}, INPUT)).servers, [{
    id: 'one', scope: 'project', state: 'stale_restart_required',
    calls: 'unavailable', tools: 0, resources: 0, prompts: 0,
  }]);
  runtime.snapshot = () => [{ id: '\u001b[0J', scope: 'project', status: 'ready' }];
  assert.equal(await liveStatusReceipt(runtime, {}, INPUT), undefined);
  runtime.isRunning = () => false;
  assert.equal(await liveStatusReceipt(runtime, {}, INPUT), undefined);
});
