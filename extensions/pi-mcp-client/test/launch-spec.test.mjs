import assert from 'node:assert/strict';
import { mkdtemp, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { test } from 'node:test';
import { prepareStdioParameters } from '../src/launch-spec.mjs';

const declaration = { id: 'local', enabled: true, instructionsPolicy: 'status-only',
  transport: { type: 'stdio', command: 'fixture-command', args: ['--safe'], cwd: 'project',
    env: { DATA_ROOT: '/example' }, envFrom: { TOKEN: 'APP_MCP_TOKEN' } } };

test('launch specification resolves only after approval and Pi trust, with safe inherited environment', async () => {
  const project = await mkdtemp(`${tmpdir()}/mcp-launch-`);
  const alternate = await mkdtemp(`${tmpdir()}/mcp-launch-alt-`);
  try {
    const projectRoot = await realpath(project);
    const entry = { projectRoot, scope: 'project', status: 'trust_required', declaration };
    const host = { PATH: '/bin', HOME: '/home/fixture', APP_MCP_TOKEN: 'secret-token',
      AMBIENT_SECRET: 'must-not-leak' };
    const ctx = { cwd: project, isProjectTrusted: () => false };
    const unreadableEnv = new Proxy({}, { get: () => { throw new Error('resolved a secret too early'); } });
    await assert.rejects(prepareStdioParameters(entry, ctx, unreadableEnv), /not authorized/);
    entry.status = 'approved';
    await assert.rejects(prepareStdioParameters(entry, ctx, unreadableEnv), /not authorized/);
    ctx.isProjectTrusted = () => true;
    ctx.cwd = alternate;
    await assert.rejects(prepareStdioParameters(entry, ctx, unreadableEnv), /context changed/);
    ctx.cwd = project;
    const params = await prepareStdioParameters(entry, ctx, host);
    assert.deepEqual(params.args, ['--safe']);
    assert.equal(params.command, 'fixture-command');
    assert.equal(params.cwd, projectRoot);
    assert.equal(params.stderr, 'pipe');
    assert.equal(params.env.TOKEN, 'secret-token');
    assert.equal(params.env.DATA_ROOT, '/example');
    assert.equal(params.env.PATH, '/bin');
    assert.equal(Object.hasOwn(params.env, 'AMBIENT_SECRET'), false);
    assert.equal(params.maxBufferSize, 2_097_152);
    await assert.rejects(prepareStdioParameters(entry, ctx, { PATH: '/bin' }), /Missing MCP host environment variable APP_MCP_TOKEN/);
    assert.deepEqual((await prepareStdioParameters({ ...entry, scope: 'user' },
      { cwd: project, isProjectTrusted: () => false }, host)).env.TOKEN, 'secret-token');
  } finally {
    await rm(project, { recursive: true });
    await rm(alternate, { recursive: true });
  }
});
