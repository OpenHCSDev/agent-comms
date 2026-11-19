import assert from 'node:assert/strict';
import { mkdir, mkdtemp, realpath, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { effectiveDeclarations, parseTrustLedger } from '../src/authority.mjs';
import { declarationDigest, parseNativeConfig } from '../src/config.mjs';
import { prepareStdioParameters } from '../src/launch-spec.mjs';
import { WorkingDirectory } from '../src/working-directory.mjs';

const config = (cwd) => parseNativeConfig(JSON.stringify({ version: 1, servers: [{
  id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
  transport: { type: 'stdio', command: process.execPath, args: ['relative.mjs'], cwd },
}] }));
const ledger = parseTrustLedger('{"version":1,"decisions":[]}');

test('directory names derive from their family; project remains the default and digest identity changes', () => {
  assert.equal(WorkingDirectory.defaultName, 'project');
  assert.deepEqual(WorkingDirectory.names(), ['project', 'agent']);
  assert.notEqual(declarationDigest(config('project').servers[0]),
    declarationDigest(config('agent').servers[0]));
  assert.throws(() => config('elsewhere'), /declaration schema/);
});

test('only explicit user-profile execution is eligible without project trust', () => {
  const common = { projectTrusted: false, projectRoot: '/project', agentDir: '/profile', ledger };
  assert.equal(effectiveDeclarations({ ...common, user: config('project') })[0].status, 'trust_required');
  const [entry] = effectiveDeclarations({ ...common, user: config('agent') });
  assert.equal(entry.status, 'approved');
  assert.equal(entry.agentDir, '/profile');
  assert.throws(() => effectiveDeclarations({ ...common, user: config('project'),
    projectTrusted: true, project: config('agent') }), /requires user scope/);
});

test('untrusted user-profile launch resolves only outside project; unsafe roots fail before environment reads', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-user-directory-'));
  const project = join(root, 'project');
  const agentDir = join(root, 'profile');
  await mkdir(project);
  await mkdir(agentDir);
  const projectRoot = await realpath(project);
  const declaration = config('agent').servers[0];
  const entry = { projectRoot, agentDir, scope: 'user', declaration, status: 'approved' };
  const ctx = { cwd: project, isProjectTrusted: () => false };
  const secrets = new Proxy({}, { get() { throw new Error('environment read too early'); } });
  try {
    const params = await prepareStdioParameters(entry, ctx, { PATH: '/usr/bin' });
    assert.equal(params.cwd, await realpath(agentDir));
    assert.deepEqual(params.args, ['relative.mjs']);
    await assert.rejects(prepareStdioParameters({ ...entry, scope: 'project' }, ctx, secrets), /requires user scope/);
    await assert.rejects(prepareStdioParameters({ ...entry, agentDir: project }, ctx, secrets), /outside the project/);
    const nested = join(project, 'profile');
    await mkdir(nested);
    await assert.rejects(prepareStdioParameters({ ...entry, agentDir: nested }, ctx, secrets), /outside the project/);
    if (process.platform !== 'win32') {
      const alias = join(root, 'alias');
      await symlink(project, alias);
      await assert.rejects(prepareStdioParameters({ ...entry, agentDir: alias }, ctx, secrets), /outside the project/);
    }
    await assert.rejects(prepareStdioParameters({ ...entry, declaration: { ...declaration,
      transport: { ...declaration.transport, command: 'node' } } }, ctx, secrets), /absolute executable/);
    await assert.rejects(prepareStdioParameters({ ...entry, status: 'trust_required' }, ctx, secrets), /not authorized/);
    await assert.rejects(prepareStdioParameters(entry, { ...ctx, cwd: agentDir }, secrets), /context changed/);
  } finally {
    await rm(root, { recursive: true });
  }
});
