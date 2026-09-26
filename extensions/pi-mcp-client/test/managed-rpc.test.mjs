import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { test } from 'node:test';

const packageDir = fileURLToPath(new URL('../', import.meta.url));
const cli = fileURLToPath(new URL('../node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js', import.meta.url));
const bootstrap = fileURLToPath(new URL('../../../src/agent_comms/pi_project_bootstrap.mjs', import.meta.url));

test('ordinary managed Pi RPC loads package but never launches a user server from untrusted project cwd', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-managed-rpc-'));
  const agentDir = join(root, 'agent');
  const project = join(root, 'project');
  const marker = join(root, 'unsafe-launch');
  await mkdir(agentDir); await mkdir(project);
  // Never inherit the managed worker's provider keys, live Pi settings,
  // NODE_OPTIONS bootstrap, or coordination identity into this fixture.
  const env = { PATH: process.env.PATH ?? '', HOME: root,
    PI_CODING_AGENT_DIR: agentDir, CI: 'true', NO_COLOR: '1',
    ...(process.platform === 'win32' ? { SystemRoot: process.env.SystemRoot ?? '' } : {}),
  };
  let child;
  try {
    const installed = spawnSync(process.execPath, [cli, 'install', packageDir], {
      cwd: project, env, encoding: 'utf8', timeout: 20_000,
    });
    assert.equal(installed.status, 0, installed.stderr?.slice(-800));
    // No .pi resources: Pi auto-trusts this cwd, but there is no explicit
    // saved user decision authorizing a global server to execute project code.
    await writeFile(join(project, 'project-controlled.mjs'),
      `import {writeFileSync} from 'node:fs'; writeFileSync(${JSON.stringify(marker)}, 'unsafe');`);
    await writeFile(join(agentDir, 'mcp.json'), JSON.stringify({ version: 1, servers: [{
      id: 'fixture', enabled: true, instructionsPolicy: 'status-only', transport: {
        type: 'stdio', command: process.execPath,
        args: ['./project-controlled.mjs'], cwd: 'project',
      },
    }] }));
    child = spawn(process.execPath, [cli, '--mode', 'rpc', '--no-session',
      '--no-skills', '--no-prompt-templates', '--no-themes', '--no-builtin-tools'], {
      cwd: project,
      env: { ...env, AGENT_COMMS_MANAGED: '1', PI_WORKTREE: project,
        NODE_OPTIONS: `--import=${pathToFileURL(bootstrap).href}` },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let output = '';
    let stderr = '';
    let statusResolver;
    child.stderr.on('data', (bytes) => { stderr += bytes.toString(); });
    const commands = new Promise((resolve, reject) => {
      child.stdout.on('data', (bytes) => {
        output += bytes.toString();
        let newline;
        while ((newline = output.indexOf('\n')) !== -1) {
          const line = output.slice(0, newline); output = output.slice(newline + 1);
          if (!line) continue;
          let message;
          try { message = JSON.parse(line); } catch (error) { reject(error); return; }
          if (message.id === 'commands') {
            if (message.success) resolve(message.data.commands.map((command) => command.name));
            else reject(new Error(JSON.stringify(message)));
          }
          if (message.type === 'extension_ui_request' && message.method === 'notify' &&
              typeof message.message === 'string' && message.message.startsWith('MCP:\n')) {
            statusResolver?.(message.message);
          }
        }
      });
      child.on('exit', (code) => reject(new Error(`managed Pi RPC exited ${code}: ${stderr.slice(-800)}`)));
    });
    child.stdin.write(JSON.stringify({ id: 'commands', type: 'get_commands' }) + '\n');
    let timer;
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`managed Pi RPC timeout: ${stderr.slice(-800)}`)), 10_000);
    });
    let result;
    try { result = await Promise.race([commands, deadline]); }
    finally { clearTimeout(timer); }
    assert.ok(result.includes('mcp-status'));
    assert.ok(result.includes('mcp-approve'));
    const status = new Promise((resolve) => { statusResolver = resolve; });
    child.stdin.write(JSON.stringify({ id: 'mcp-status', type: 'prompt', message: '/mcp-status' }) + '\n');
    let statusTimer;
    const observed = await Promise.race([status, new Promise((_, reject) => {
      statusTimer = setTimeout(() => reject(new Error('managed Pi MCP status timed out')), 5000);
    })]).finally(() => clearTimeout(statusTimer));
    assert.match(observed, /user\/fixture: trust_required; calls=unavailable/);
    assert.equal(existsSync(marker), false);
  } finally {
    if (child) {
      child.stdin.destroy();
      if (child.exitCode === null && child.signalCode === null) {
        const exited = new Promise((resolve) => child.once('exit', resolve));
        const waitForExit = async () => {
          let timer;
          try { return await Promise.race([exited.then(() => true), new Promise((resolve) => {
            timer = setTimeout(() => resolve(false), 2000);
          })]); }
          finally { clearTimeout(timer); }
        };
        child.kill();
        if (!await waitForExit()) {
          child.kill('SIGKILL');
          if (!await waitForExit()) throw new Error('managed Pi RPC child did not exit');
        }
      }
    }
    await rm(root, { recursive: true, force: true });
  }
});
