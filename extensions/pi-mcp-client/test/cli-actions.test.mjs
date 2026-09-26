import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { ProjectTrustStore } from '@earendil-works/pi-coding-agent';
import { writeNativeServer } from '../src/config-write.mjs';

const cli = fileURLToPath(new URL('../bin/pi-mcp.mjs', import.meta.url));
const script = spawnSync('sh', ['-c', 'command -v script'], { encoding: 'utf8' }).stdout.trim();
const shellQuote = (value) => `'${value.replaceAll("'", "'\\''")}'`;

test('CLI binds local approval/call decisions to exact digest; unattended writes fail closed',
  { skip: process.platform === 'win32' || !script }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-cli-decisions-'));
  const agentDir = join(root, 'agent');
  const projectRoot = join(root, 'project');
  const marker = join(root, 'unexpected-server-spawn');
  await mkdir(projectRoot);
  const env = { PATH: process.env.PATH ?? '', HOME: root,
    PI_CODING_AGENT_DIR: agentDir, CI: 'true', NO_COLOR: '1' };
  const run = (...args) => spawnSync(process.execPath, [cli, ...args], {
    cwd: projectRoot, env, input: '', encoding: 'utf8', timeout: 5000,
  });
  const interactive = (challenge, ...args) => spawnSync(script,
    ['-q', '-e', '-c', [process.execPath, cli, ...args].map(shellQuote).join(' '), '/dev/null'],
    { cwd: projectRoot, env, input: `${challenge}\n`, encoding: 'utf8', timeout: 5000 });
  try {
    new ProjectTrustStore(agentDir).set(projectRoot, true);
    await writeNativeServer({ agentDir, projectRoot, configDirName: '.pi', scope: 'project',
      declaration: { id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
        transport: { type: 'stdio', command: process.execPath,
          args: ['-e', `require('fs').writeFileSync(${JSON.stringify(marker)}, 'bad')`],
          cwd: 'project' } } });
    const before = JSON.parse(run('inventory', '--json').stdout);
    const digest = before.declarations.project[0].digest;
    const stale = interactive(`approve:fixture:${'0'.repeat(64)}`, 'trust', 'approve',
      '--id', 'fixture', '--digest', '0'.repeat(64));
    assert.equal(stale.status, 1);
    assert.match(stale.stdout, /digest changed before approval/);
    let snapshot = JSON.parse(run('inventory', '--json').stdout);
    assert.equal(snapshot.declarations.project[0].status, 'trust_required');
    const unattended = run('trust', 'approve', '--id', 'fixture', '--digest', digest);
    assert.equal(unattended.status, 1);
    assert.match(unattended.stderr, /interactive local TTY/);
    const approved = interactive(`approve:fixture:${digest}`, 'trust', 'approve',
      '--id', 'fixture', '--digest', digest);
    assert.equal(approved.status, 0, approved.stdout + approved.stderr);
    snapshot = JSON.parse(run('inventory', '--json').stdout);
    assert.equal(snapshot.declarations.project[0].status, 'approved');
    assert.equal(snapshot.declarations.project[0].callPolicy, 'ask');
    const granted = interactive(`allow:fixture:${digest}`, 'calls', 'allow',
      '--id', 'fixture', '--digest', digest);
    assert.equal(granted.status, 0, granted.stdout + granted.stderr);
    snapshot = JSON.parse(run('inventory', '--json').stdout);
    assert.equal(snapshot.declarations.project[0].callPolicy, 'allow');
    const revoked = interactive(`deny:fixture:${digest}`, 'trust', 'deny',
      '--id', 'fixture', '--digest', digest);
    assert.equal(revoked.status, 0, revoked.stdout + revoked.stderr);
    snapshot = JSON.parse(run('inventory', '--json').stdout);
    assert.equal(snapshot.declarations.project[0].status, 'denied');
    assert.equal(snapshot.declarations.project[0].callPolicy, 'unavailable');
    const reapproved = interactive(`approve:fixture:${digest}`, 'trust', 'approve',
      '--id', 'fixture', '--digest', digest);
    assert.equal(reapproved.status, 0, reapproved.stdout + reapproved.stderr);
    snapshot = JSON.parse(run('inventory', '--json').stdout);
    assert.equal(snapshot.declarations.project[0].status, 'approved');
    assert.equal(snapshot.declarations.project[0].callPolicy, 'ask');
    assert.equal(existsSync(marker), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
