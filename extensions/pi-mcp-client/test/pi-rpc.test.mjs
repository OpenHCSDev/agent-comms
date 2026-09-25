import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { CONFIG_DIR_NAME } from '@earendil-works/pi-coding-agent';
import { declarationDigest, parseNativeConfig } from '../src/config.mjs';

const packageDir = dirname(dirname(fileURLToPath(import.meta.url)));
const cli = join(packageDir, 'node_modules', '@earendil-works', 'pi-coding-agent', 'dist', 'bundle', 'cli.js');

async function rpc({ project, agentDir, trust }) {
  // Deliberately do not inherit model API keys, live Pi settings, or user extensions.
  const child = spawn(process.execPath, [cli, '--mode', 'rpc', '--no-session',
    '--no-extensions', '--no-skills', '--no-prompt-templates', '--no-themes',
    '--no-context-files', '--no-builtin-tools',
    trust ? '--approve' : '--no-approve', '-e', packageDir], {
    cwd: project,
    env: { PATH: process.env.PATH ?? '', HOME: dirname(agentDir),
      PI_CODING_AGENT_DIR: agentDir, CI: 'true', NO_COLOR: '1' },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const pending = new Map();
  const closed = new Promise((resolve) => child.on('exit', (code, signal) => {
    for (const answer of pending.values()) answer({ success: false, code, signal, stderr });
    pending.clear();
    resolve();
  }));
  const events = [];
  let stderr = '';
  child.stderr.on('data', (chunk) => { stderr = (stderr + chunk.toString()).slice(-4000); });
  let buffer = '';
  child.stdout.on('data', (chunk) => {
    buffer += chunk.toString('utf8');
    for (let index; (index = buffer.indexOf('\n')) !== -1;) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (!line) continue;
      const message = JSON.parse(line);
      if (message.type === 'response' && pending.has(message.id)) {
        pending.get(message.id)(message);
        pending.delete(message.id);
      } else events.push(message);
    }
  });
  let number = 0;
  async function request(type, extras = {}) {
    const id = `test-${++number}`;
    const awaited = new Promise((resolve) => pending.set(id, resolve));
    child.stdin.write(JSON.stringify({ id, type, ...extras }) + '\n');
    let timer;
    const timeout = new Promise((_, reject) => { timer = setTimeout(() =>
      reject(new Error(`Pi RPC ${type} timeout: ${stderr}`)), 10_000); });
    let response;
    try { response = await Promise.race([awaited, timeout]); }
    finally { clearTimeout(timer); }
    assert.equal(response.success, true, JSON.stringify(response));
    return response;
  }
  return { child, closed, request, events, stderr: () => stderr };
}

test('real isolated Pi RPC trusts project only by Pi policy and never starts declared servers', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-pi-rpc-'));
  const project = join(root, 'project');
  const agentDir = join(root, 'agent');
  const marker = join(root, 'unexpected-launch');
  const projectFile = join(project, CONFIG_DIR_NAME, 'mcp.json');
  await mkdir(dirname(projectFile), { recursive: true });
  await mkdir(agentDir);
  const declaration = { id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
    transport: { type: 'stdio', command: process.execPath,
      args: ['-e', `require('fs').writeFileSync(${JSON.stringify(marker)},'spawned')`],
      cwd: 'project' } };
  const document = JSON.stringify({ version: 1, servers: [declaration] });
  await writeFile(projectFile, document);
  try {
    for (const [trusted, approval, expected] of [
      [false, false, 'No MCP declarations'],
      [true, false, 'trust_required'],
      [true, true, 'approved'],
    ]) {
      // A malformed untrusted project file must not even be parsed by Pi's command.
      await writeFile(projectFile, trusted ? document : '{bad');
      if (approval) await writeFile(join(agentDir, 'mcp-trust.json'), JSON.stringify({
        version: 1, decisions: [{ projectRoot: await realpath(project), scope: 'project', serverId: 'fixture',
          digest: declarationDigest(parseNativeConfig(document).servers[0]), decision: 'approve' }],
      }));
      const client = await rpc({ project, agentDir, trust: trusted });
      try {
        const commands = (await client.request('get_commands')).data.commands.map((entry) => entry.name);
        assert.ok(commands.includes('mcp-status'), JSON.stringify(commands));
        assert.ok(commands.includes('mcp-approve'), JSON.stringify(commands));
        await client.request('prompt', { message: '/mcp-status' });
        assert.equal(client.events.some((message) =>
          message.type === 'extension_ui_request' &&
          JSON.stringify(message).includes(expected)), true,
        `Missing ${expected}: ${JSON.stringify(client.events)}; stderr=${client.stderr()}`);
        if (trusted && !approval) {
          // RPC's hasUI=true is NOT evidence that a human can answer a dialog.
          // This command must fail before writing, regardless of Pi prompt disposition.
          try { await client.request('prompt', { message: '/mcp-approve fixture' }); }
          catch (error) { assert.match(error.message, /local interactive TUI|success.*false/); }
          assert.equal(existsSync(join(agentDir, 'mcp-trust.json')), false);
        }
        assert.equal(existsSync(marker), false);
      } finally {
        client.child.stdin.end();
        client.child.kill();
        await client.closed;
      }
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
