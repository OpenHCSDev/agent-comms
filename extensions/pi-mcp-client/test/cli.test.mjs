import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { ProjectTrustStore } from '@earendil-works/pi-coding-agent';
import { writeNativeServer } from '../src/config-write.mjs';

const cli = fileURLToPath(new URL('../bin/pi-mcp.mjs', import.meta.url));

test('CLI redacts literal env values, reports saved trust, refuses unattended writes', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-cli-'));
  const agentDir = join(root, 'agent');
  const projectRoot = join(root, 'project');
  const marker = join(root, 'unexpected-launch');
  await mkdir(projectRoot);
  const call = (...args) => spawnSync(process.execPath, [cli, ...args], {
    cwd: projectRoot, env: { PATH: process.env.PATH ?? '', HOME: root,
      PI_CODING_AGENT_DIR: agentDir, CI: 'true', NO_COLOR: '1',
      ...(process.platform === 'win32' ? { SystemRoot: process.env.SystemRoot ?? '' } : {}),
    },
    input: 'fixture:0123456789ab\n', encoding: 'utf8', timeout: 5000,
  });
  try {
    const addArgs = ['add', '--scope', 'user', '--id', 'fixture',
      '--command', process.execPath, '--arg', '-e', '--arg',
      `require('fs').writeFileSync(${JSON.stringify(marker)}, 'launched')`,
      '--env', 'API_TOKEN=do-not-display-secret', '--env-from', 'PASS_THROUGH=SYSTEM_TOKEN'];
    const preview = call(...addArgs, '--dry-run');
    assert.equal(preview.status, 0, preview.stderr);
    assert.equal(JSON.parse(preview.stdout).applied, false);
    assert.doesNotMatch(preview.stdout, /do-not-display-secret/);
    const projectLiteral = call(...addArgs.map((value) => value === 'user' ? 'project' : value), '--dry-run');
    assert.equal(projectLiteral.status, 1);
    assert.match(projectLiteral.stderr, /literal environment is not supported/);
    const denied = call(...addArgs);
    assert.equal(denied.status, 1);
    assert.match(denied.stderr, /interactive local TTY/);
    assert.equal(existsSync(join(projectRoot, '.pi', 'mcp.json')), false);
    const declaration = { id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
      transport: { type: 'stdio', command: process.execPath, args: [], cwd: 'project' } };
    await mkdir(join(projectRoot, '.pi'));
    await writeFile(join(projectRoot, '.pi', 'mcp.json'),
      JSON.stringify({ version: 1, servers: [declaration] }));
    let status = call('status', '--json');
    assert.equal(status.status, 0, status.stderr);
    let json = JSON.parse(status.stdout);
    assert.equal(json.projectTrustedSaved, false);
    assert.equal(json.projectConfigSkipped, true);
    assert.deepEqual(json.servers, []);
    let inventory = call('inventory', '--json');
    assert.equal(inventory.status, 0, inventory.stderr);
    let listed = JSON.parse(inventory.stdout);
    assert.equal(listed.version, 2);
    assert.deepEqual(listed.compatibility, {
      version: 1, positiveDecisions: 'locked-project-approval-v1',
    });
    assert.equal(listed.projectConfigSkipped, true);
    assert.deepEqual(listed.declarations.project, []);
    assert.equal(listed.live.state, 'not_running');
    await writeNativeServer({ agentDir, projectRoot, configDirName: '.pi', scope: 'user', declaration });
    status = call('status', '--json');
    assert.equal(status.status, 0, status.stderr);
    json = JSON.parse(status.stdout);
    assert.equal(json.servers[0].status, 'trust_required');
    assert.equal(json.servers[0].scope, 'user');
    inventory = call('inventory', '--json');
    listed = JSON.parse(inventory.stdout);
    assert.equal(listed.declarations.user[0].status, 'trust_required');
    assert.deepEqual(listed.declarations.project, []);
    new ProjectTrustStore(agentDir).set(projectRoot, true);
    status = call('status', '--json');
    assert.equal(status.status, 0, status.stderr);
    json = JSON.parse(status.stdout);
    assert.equal(json.projectTrustedSaved, true);
    assert.equal(json.servers.length, 1);
    assert.equal(json.servers[0].scope, 'project');
    assert.equal(json.servers[0].status, 'trust_required');
    inventory = call('inventory', '--json');
    listed = JSON.parse(inventory.stdout);
    assert.equal(listed.projectTrustedSaved, true);
    assert.equal(listed.declarations.user[0].status, 'shadowed');
    assert.equal(listed.declarations.user[0].effective, false);
    assert.equal(listed.declarations.project[0].status, 'trust_required');
    assert.equal(listed.declarations.project[0].effective, true);
    assert.equal(listed.declarations.project[0].callPolicy, 'unavailable');
    assert.equal(listed.declarations.project[0].digest, json.servers[0].digest);
    assert.equal(inventory.stdout.includes('do-not-display-secret'), false);
    assert.equal(existsSync(marker), false);
    assert.doesNotMatch(await readFile(join(agentDir, 'mcp.json'), 'utf8'), /do-not-display-secret/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
