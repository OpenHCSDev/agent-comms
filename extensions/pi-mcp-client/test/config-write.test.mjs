import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { ProjectTrustStore } from '@earendil-works/pi-coding-agent';
import { writeNativeServer } from '../src/config-write.mjs';
import { parseNativeConfig } from '../src/config.mjs';
import { loadEffectiveDeclarations } from '../src/sources.mjs';

const declaration = (command) => ({ id: 'fixture', enabled: true,
  instructionsPolicy: 'status-only', transport: { type: 'stdio', command,
    args: [], cwd: 'project' } });

test('native config writer adds/replaces whole declarations without launch or self-approval', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-config-write-'));
  const agentDir = join(root, 'agent');
  const projectRoot = join(root, 'project');
  const marker = join(root, 'unexpected-launch');
  await mkdir(projectRoot);
  try {
    const input = declaration(process.execPath);
    input.transport.args = ['-e', `require('fs').writeFileSync(${JSON.stringify(marker)},'launched')`];
    const opts = { agentDir, projectRoot, configDirName: '.pi', scope: 'project' };
    await assert.rejects(writeNativeServer({ ...opts, declaration: input }), /Saved Pi project trust required/);
    assert.equal(existsSync(join(projectRoot, '.pi', 'mcp.json')), false);
    await mkdir(agentDir, { recursive: true });
    new ProjectTrustStore(agentDir).set(projectRoot, true);
    const added = await writeNativeServer({ ...opts, declaration: input });
    assert.equal(added.id, 'fixture');
    assert.equal(existsSync(marker), false);
    assert.equal((await loadEffectiveDeclarations({ agentDir, configDirName: '.pi',
      ctx: { cwd: projectRoot, isProjectTrusted: () => true } }))[0].status, 'trust_required');
    await assert.rejects(writeNativeServer({ ...opts, declaration: input }), /already exists/);
    await assert.rejects(writeNativeServer({ ...opts, declaration: declaration('other'), replace: false }), /already exists/);
    const replaced = await writeNativeServer({ ...opts,
      declaration: declaration('other'), replace: true });
    assert.notEqual(added.digest, replaced.digest);
    const current = parseNativeConfig(await readFile(added.configPath, 'utf8'));
    assert.equal(current.servers.length, 1);
    assert.equal(current.servers[0].transport.command, 'other');
    assert.equal(existsSync(marker), false);
    const user = await writeNativeServer({ ...opts, scope: 'user', declaration: input });
    assert.equal(parseNativeConfig(await readFile(user.configPath, 'utf8')).servers[0].id, 'fixture');
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('native config writer refuses a project config directory symlink', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-config-write-'));
  const projectRoot = join(root, 'project');
  const elsewhere = join(root, 'elsewhere');
  await mkdir(projectRoot); await mkdir(elsewhere);
  await symlink(elsewhere, join(projectRoot, '.pi'));
  await mkdir(join(root, 'agent'));
  new ProjectTrustStore(join(root, 'agent')).set(projectRoot, true);
  try {
    await assert.rejects(writeNativeServer({ agentDir: join(root, 'agent'), projectRoot,
      configDirName: '.pi', scope: 'project', declaration: declaration('noop') }), /symlink refused/);
    assert.equal(existsSync(join(elsewhere, 'mcp.json')), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
