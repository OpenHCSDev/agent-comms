import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { declarationDigest, parseNativeConfig } from '../src/config.mjs';
import { loadEffectiveDeclarations } from '../src/sources.mjs';

const config = (servers) => JSON.stringify({ version: 1, servers });
const server = (command) => ({ id: 'local', enabled: true, instructionsPolicy: 'status-only',
  transport: { type: 'stdio', command, args: [], cwd: 'project' } });

test('Pi project trust gates reading; exact external digest gates eligibility; neither starts a child', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-sources-'));
  const project = join(root, 'project');
  const agentDir = join(root, 'agent');
  const marker = join(root, 'unexpected-launch');
  await mkdir(join(project, '.pi'), { recursive: true });
  await mkdir(agentDir);
  try {
    const user = server('user-server');
    const projectServer = server(process.execPath);
    projectServer.transport.args = ['-e', `require('fs').writeFileSync(${JSON.stringify(marker)},'spawned')`];
    await writeFile(join(agentDir, 'mcp.json'), config([user]));
    const projectPath = join(project, '.pi', 'mcp.json');
    await writeFile(projectPath, '{bad');
    const load = (trusted) => loadEffectiveDeclarations({
      ctx: { cwd: project, isProjectTrusted: () => trusted }, agentDir, configDirName: '.pi',
    });
    assert.deepEqual((await load(false)).map(({ scope, status }) => [scope, status]), [['user', 'approved']]);
    await assert.rejects(load(true), /Invalid MCP config: JSON syntax/);
    await writeFile(projectPath, config([projectServer]));
    assert.deepEqual((await load(true)).map(({ scope, status }) => [scope, status]), [['project', 'trust_required']]);
    const digest = declarationDigest(parseNativeConfig(config([projectServer])).servers[0]);
    await writeFile(join(agentDir, 'mcp-trust.json'), JSON.stringify({ version: 1, decisions: [
      { projectRoot: await realpath(project), scope: 'project', serverId: 'local', digest, decision: 'approve' },
    ] }));
    assert.deepEqual((await load(true)).map(({ scope, status }) => [scope, status]), [['project', 'approved']]);
    await writeFile(projectPath, config([{ ...projectServer, transport: { ...projectServer.transport, args: ['changed'] } }]));
    assert.equal((await load(true))[0].status, 'trust_required');
    assert.equal(existsSync(marker), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('user declarations cannot be symlinked to project-controlled config', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-sources-'));
  const agentDir = join(root, 'agent');
  const project = join(root, 'project');
  await mkdir(agentDir);
  await mkdir(project);
  try {
    const external = join(project, 'injected.json');
    await writeFile(external, config([server('project-injected')]));
    await symlink(external, join(agentDir, 'mcp.json'));
    await assert.rejects(loadEffectiveDeclarations({
      ctx: { cwd: project, isProjectTrusted: () => false }, agentDir, configDirName: '.pi',
    }), /symlink refused/);
    assert.equal((await readFile(external, 'utf8')).includes('project-injected'), true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
