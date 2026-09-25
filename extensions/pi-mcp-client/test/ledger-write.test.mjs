import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { parseTrustLedger } from '../src/authority.mjs';
import { parseNativeConfig } from '../src/config.mjs';
import { recordProjectDecision } from '../src/ledger-write.mjs';
import { loadEffectiveDeclarations } from '../src/sources.mjs';

const make = (command) => ({ id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
  transport: { type: 'stdio', command, args: [], cwd: 'project' } });
const config = (declaration) => JSON.stringify({ version: 1, servers: [declaration] });

test('user-authorized atomic decision revokes prior digests; malformed ledger and lock fail closed',
  { skip: process.platform === 'win32' && 'Windows durable decision writes are disabled' }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-ledger-'));
  const agentDir = join(root, 'agent');
  const projectRoot = join(root, 'project');
  await mkdir(agentDir);
  await mkdir(join(projectRoot, '.pi'), { recursive: true });
  const projectPath = join(projectRoot, '.pi', 'mcp.json');
  const ledgerPath = join(agentDir, 'mcp-trust.json');
  const load = () => loadEffectiveDeclarations({ ctx: {
    cwd: projectRoot, isProjectTrusted: () => true }, agentDir, configDirName: '.pi' });
  try {
    const first = make('first-command');
    const second = make('second-command');
    await writeFile(projectPath, config(first));
    assert.equal((await load())[0].status, 'trust_required');
    await recordProjectDecision({ agentDir, projectRoot, declaration: first, decision: 'approve' });
    assert.equal((await load())[0].status, 'approved');
    await writeFile(projectPath, config(second));
    assert.equal((await load())[0].status, 'trust_required');
    await recordProjectDecision({ agentDir, projectRoot, declaration: second, decision: 'approve' });
    assert.equal((await load())[0].status, 'approved');
    await writeFile(projectPath, config(first));
    assert.equal((await load())[0].status, 'trust_required'); // No implicit rollback grant.
    await recordProjectDecision({ agentDir, projectRoot, declaration: first, decision: 'deny' });
    assert.equal((await load())[0].status, 'denied');
    const parsed = parseTrustLedger(await readFile(ledgerPath, 'utf8'));
    assert.equal(parsed.decisions.length, 1);
    assert.equal(parsed.decisions[0].decision, 'deny');
    if (process.platform !== 'win32') {
      const { mode } = await stat(ledgerPath);
      assert.equal(mode & 0o077, 0);
    }
    await mkdir(ledgerPath + '.lock');
    await assert.rejects(recordProjectDecision({
      agentDir, projectRoot, declaration: first, decision: 'approve',
    }), (error) => error.code === 'EEXIST');
    await rm(ledgerPath + '.lock', { recursive: true });
    await writeFile(ledgerPath, '{invalid');
    await assert.rejects(recordProjectDecision({
      agentDir, projectRoot, declaration: first, decision: 'approve',
    }), /Invalid MCP trust ledger: JSON syntax/);
    assert.equal(await readFile(ledgerPath, 'utf8'), '{invalid');
  } finally {
    await rm(root, { force: true, recursive: true });
  }
});

test('writer rejects nonexistent project and unsupported decision before taking a lock', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-ledger-'));
  try {
    const declaration = parseNativeConfig(config(make('no-launch'))).servers[0];
    await assert.rejects(recordProjectDecision({
      agentDir: root, projectRoot: join(root, 'nonexistent'), declaration, decision: 'approve',
    }), (error) => error.code === 'ENOENT');
    await assert.rejects(recordProjectDecision({
      agentDir: root, projectRoot: root, declaration, decision: 'auto-approve',
    }), /Invalid MCP trust ledger: schema/);
  } finally {
    await rm(root, { force: true, recursive: true });
  }
});
