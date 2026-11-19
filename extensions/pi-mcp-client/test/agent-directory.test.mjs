import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { getAgentDir, ProjectTrustStore } from '@earendil-works/pi-coding-agent';
import extension from '../index.mjs';
import { getMcpAgentDir } from '../src/agent-directory.mjs';

const cli = fileURLToPath(new URL('../bin/pi-mcp.mjs', import.meta.url));

async function withDirectories(run) {
  const root = await mkdtemp(join(tmpdir(), 'mcp-user-directory-'));
  const user = join(root, 'user');
  const isolated = join(root, 'isolated');
  const project = join(root, 'project');
  const previousAgent = process.env.PI_CODING_AGENT_DIR;
  const previousNative = process.env.AGENT_COMMS_NATIVE_CONFIG_DIR;
  await Promise.all([user, isolated, project].map((path) => mkdir(path)));
  process.env.PI_CODING_AGENT_DIR = isolated;
  process.env.AGENT_COMMS_NATIVE_CONFIG_DIR = user;
  try {
    await run({ root, user, isolated, project });
  } finally {
    if (previousAgent === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = previousAgent;
    if (previousNative === undefined) delete process.env.AGENT_COMMS_NATIVE_CONFIG_DIR;
    else process.env.AGENT_COMMS_NATIVE_CONFIG_DIR = previousNative;
    await rm(root, { recursive: true, force: true });
  }
}

function declaration(enabled) {
  return { version: 1, servers: [{ id: 'probe', enabled, instructionsPolicy: 'status-only',
    transport: { type: 'stdio', command: process.execPath, args: [], cwd: 'project' } }] };
}

async function extensionStatus(project) {
  const events = new Map();
  const commands = new Map();
  const notifications = [];
  extension({
    on(name, handler) { events.set(name, handler); },
    registerCommand(name, command) { commands.set(name, command); },
    getAllTools() { return []; },
    registerTool() { throw new Error('an ineligible fixture must expose no tools'); },
  });
  const ctx = { cwd: project, mode: 'rpc', isProjectTrusted: () => false,
    ui: { notify(message) { notifications.push(message); } } };
  try {
    await events.get('session_start')({}, ctx);
    await commands.get('mcp-status').handler('', ctx);
    return notifications;
  } finally {
    await events.get('session_shutdown')({}, ctx);
  }
}

test('native MCP uses the original directory while stock Pi keeps its agent directory', async () => {
  await withDirectories(async ({ user, isolated }) => {
    assert.equal(getMcpAgentDir(), user);
    assert.equal(getAgentDir(), isolated); // writable Pi settings remain isolated
    delete process.env.AGENT_COMMS_NATIVE_CONFIG_DIR;
    assert.equal(getMcpAgentDir(), isolated);
    process.env.AGENT_COMMS_NATIVE_CONFIG_DIR = 'project-controlled-relative';
    assert.throws(getMcpAgentDir, /Absolute MCP user configuration/);
    process.env.AGENT_COMMS_NATIVE_CONFIG_DIR = '';
    assert.throws(getMcpAgentDir, /Absolute MCP user configuration/);
  });
});

test('actual CLI and extension read the same user declaration without copying private settings', async () => {
  await withDirectories(async ({ root, user, isolated, project }) => {
    const document = JSON.stringify(declaration(false));
    await writeFile(join(user, 'mcp.json'), document);
    const settings = '{"steeringMode":"all"}\n';
    await writeFile(join(isolated, 'settings.json'), settings);
    const done = spawnSync(process.execPath, [cli, 'status', '--json', '--project', project], {
      cwd: project, encoding: 'utf8', timeout: 5000,
      env: { PATH: process.env.PATH ?? '', HOME: root, PI_CODING_AGENT_DIR: isolated,
        AGENT_COMMS_NATIVE_CONFIG_DIR: user, CI: 'true', NO_COLOR: '1',
        ...(process.platform === 'win32' ? { SystemRoot: process.env.SystemRoot ?? '' } : {}) },
    });
    assert.equal(done.status, 0, done.stderr);
    const status = JSON.parse(done.stdout);
    assert.equal(status.projectTrustedSaved, false);
    assert.deepEqual(status.servers.map(({ id, scope, status }) => ({ id, scope, status })),
      [{ id: 'probe', scope: 'user', status: 'disabled' }]);
    assert.match((await extensionStatus(project)).join('\n'), /user\/probe: disabled; calls=unavailable/);
    assert.equal(await readFile(join(isolated, 'settings.json'), 'utf8'), settings);
    assert.equal(await readFile(join(user, 'mcp.json'), 'utf8'), document);
    await assert.rejects(readFile(join(isolated, 'mcp.json')), { code: 'ENOENT' });
    await assert.rejects(readFile(join(isolated, 'mcp-trust.json')), { code: 'ENOENT' });
  });
});

test('original saved trust does not override an untrusted native session context', async () => {
  await withDirectories(async ({ user, project }) => {
    await writeFile(join(user, 'mcp.json'), JSON.stringify(declaration(true)));
    new ProjectTrustStore(user).set(project, true);
    const messages = await extensionStatus(project);
    assert.match(messages.join('\n'), /user\/probe: trust_required; calls=unavailable/);
    assert.equal(new ProjectTrustStore(user).get(project), true);
  });
});
