import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { parseTrustLedger } from '../src/authority.mjs';
import { updateJsonFile, syncParentDirectory } from '../src/atomic-file.mjs';
import { declarationDigest } from '../src/config.mjs';
import { recordProjectDecision } from '../src/ledger-write.mjs';
import { loadEffectiveDeclarations, readTrustLedger } from '../src/sources.mjs';

const declaration = { id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
  transport: { type: 'stdio', command: 'fixture-command', args: [], cwd: 'project' } };

test('post-rename fsync failure leaves a durable uncertainty marker; no grant is usable',
  { skip: process.platform === 'win32' && 'Windows durable decision writes are disabled' }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-ledger-uncertain-'));
  const agentDir = join(root, 'agent');
  const projectRoot = join(root, 'project');
  await mkdir(agentDir); await mkdir(join(projectRoot, '.pi'), { recursive: true });
  const path = join(agentDir, 'mcp-trust.json');
  await writeFile(join(projectRoot, '.pi', 'mcp.json'),
    JSON.stringify({ version: 1, servers: [declaration] }));
  const decision = { projectRoot, scope: 'project', serverId: 'fixture',
    digest: declarationDigest(declaration), decision: 'approve' };
  try {
    let syncs = 0;
    await assert.rejects(updateJsonFile(path, {
      initial: '{"version":1,"decisions":[]}', parse: parseTrustLedger, mode: 0o600,
      durableAuthority: true,
      update: (current) => ({ ...current, decisions: [decision] }),
      syncDirectory: async (file) => {
        if (++syncs === 2) throw new Error('injected after rename');
        await syncParentDirectory(file);
      },
    }), /directory durability is unknown/);
    assert.equal(syncs, 2);
    assert.equal(parseTrustLedger(await readFile(path, 'utf8')).decisions[0].decision, 'approve');
    await assert.rejects(loadEffectiveDeclarations({ agentDir, configDirName: '.pi',
      ctx: { cwd: projectRoot, isProjectTrusted: () => true } }), /durability is uncertain/);
    await assert.rejects(recordProjectDecision({ agentDir, projectRoot,
      declaration, decision: 'deny' }), (error) => error.code === 'EEXIST');
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('failed lock cleanup after rename leaves active bytes quarantined',
  { skip: process.platform === 'win32' && 'Windows durable decision writes are disabled' }, async () => {
  const agentDir = await mkdtemp(join(tmpdir(), 'mcp-ledger-lock-'));
  const path = join(agentDir, 'mcp-trust.json');
  try {
    let syncs = 0;
    await assert.rejects(updateJsonFile(path, {
      initial: '{"version":1,"decisions":[]}', parse: parseTrustLedger, mode: 0o600,
      durableAuthority: true,
      update: () => ({ version: 1, decisions: [{ projectRoot: agentDir, scope: 'project',
        serverId: 'fixture', digest: declarationDigest(declaration), decision: 'approve' }] }),
      syncDirectory: async (file) => {
        await syncParentDirectory(file);
        if (++syncs === 2) await rm(path + '.lock', { recursive: true });
      },
    }), (error) => error.code === 'ENOENT');
    assert.equal(syncs, 2);
    assert.equal(parseTrustLedger(await readFile(path, 'utf8')).decisions.length, 1);
    await assert.rejects(readTrustLedger(agentDir), /durability is uncertain/);
  } finally {
    await rm(agentDir, { recursive: true, force: true });
  }
});

test('Windows rejects durable decisions rather than reporting a revocable grant',
  { skip: process.platform !== 'win32' && 'Windows-specific failure contract' }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-ledger-win-'));
  try {
    await assert.rejects(recordProjectDecision({ agentDir: root, projectRoot: root,
      declaration, decision: 'approve' }), /unavailable on Windows/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
