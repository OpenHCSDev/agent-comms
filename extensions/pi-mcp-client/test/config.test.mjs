import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { declarationDigest, parseNativeConfig } from '../src/config.mjs';

const server = {
  id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
  transport: { type: 'stdio', command: 'fixture-server', args: [], cwd: 'project',
    env: { DATA_ROOT: '/example' }, envFrom: { TOKEN: 'MCP_TEST_TOKEN' } },
};
const config = (servers) => JSON.stringify({ version: 1, servers });

test('inert strict native config cannot authorize itself or launch its declared child', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-config-'));
  const marker = join(root, 'unexpected-launch');
  try {
    const inert = structuredClone(server);
    inert.transport.command = process.execPath;
    inert.transport.args = ['-e', `require('fs').writeFileSync(${JSON.stringify(marker)}, 'launched')`];
    const parsed = parseNativeConfig(config([inert]));
    assert.equal(parsed.servers[0].id, 'fixture');
    assert.equal(existsSync(marker), false);
    for (const input of [
      config([{ ...server, approved: true }]),
      config([{ ...server, transport: { ...server.transport, shell: true } }]),
      config([{ ...server, transport: { ...server.transport, type: 'http' } }]),
      config([{ ...server, transport: { ...server.transport, envFrom: { DATA_ROOT: 'SECRET' } } }]),
      config([server, server]),
      JSON.stringify({ version: 1, servers: [server], trust: 'yes' }),
    ]) assert.throws(() => parseNativeConfig(input), /Invalid MCP config: declaration schema/);
    assert.equal(existsSync(marker), false);
  } finally {
    await rm(root, { recursive: true });
  }
});

test('declaration digests are stable by JSON key order and change with executable authority', () => {
  const parsed = parseNativeConfig(config([server])).servers[0];
  const reordered = parseNativeConfig(config([{
    transport: { envFrom: { TOKEN: 'MCP_TEST_TOKEN' }, env: { DATA_ROOT: '/example' },
      cwd: 'project', args: [], command: 'fixture-server', type: 'stdio' },
    instructionsPolicy: 'status-only', enabled: true, id: 'fixture',
  }])).servers[0];
  assert.equal(declarationDigest(parsed), declarationDigest(reordered));
  for (const candidate of [
    { ...server, transport: { ...server.transport, command: 'other' } },
    { ...server, transport: { ...server.transport, args: ['--new'] } },
    { ...server, transport: { ...server.transport, envFrom: { TOKEN: 'OTHER_SECRET' } } },
    { ...server, enabled: false },
  ]) assert.notEqual(declarationDigest(parsed), declarationDigest(candidate));
});

test('size and malformed declarations fail without exposing literal secrets', () => {
  for (const text of [
    '{"version":1,"servers":[{"secret":"never-expose-me"}',
    config([{ ...server, transport: { ...server.transport, env: { TOKEN: 'never-expose-me' } } }]),
    'x'.repeat(120_001),
  ]) {
    assert.throws(() => parseNativeConfig(text), (error) =>
      error.message.startsWith('Invalid MCP config:') && !error.message.includes('never-expose-me'));
  }
});
