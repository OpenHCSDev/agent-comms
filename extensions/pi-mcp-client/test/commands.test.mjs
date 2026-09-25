import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { decideProjectServer } from '../src/commands.mjs';
import { loadEffectiveDeclarations } from '../src/sources.mjs';

const declaration = (command) => ({ id: 'one', enabled: true, instructionsPolicy: 'status-only',
  transport: { type: 'stdio', command, args: ['--safe'], cwd: 'project',
    env: { PRIVATE: 'literal-secret-never-display' }, envFrom: { TOKEN: 'MCP_TOKEN' } } });
const config = (entry) => JSON.stringify({ version: 1, servers: [entry] });

test('only a Pi-trusted local TUI can explicitly approve exact currently displayed project bytes', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-command-'));
  const agentDir = join(root, 'agent');
  const project = join(root, 'project');
  const file = join(project, '.pi', 'mcp.json');
  const ledger = join(agentDir, 'mcp-trust.json');
  await mkdir(agentDir);
  await mkdir(join(project, '.pi'), { recursive: true });
  await writeFile(file, config(declaration('fixture-command')));
  let trusted = false;
  let confirmations = 0;
  let prompt = '';
  let confirm = async (_, message) => { confirmations++; prompt = message; return false; };
  const ctx = { mode: 'rpc', cwd: project, isProjectTrusted: () => trusted,
    ui: { confirm: (...args) => confirm(...args) } };
  const options = { agentDir, configDirName: '.pi', id: 'one', decision: 'approve' };
  try {
    await assert.rejects(decideProjectServer(ctx, options), /local interactive TUI/);
    ctx.mode = 'tui';
    await assert.rejects(decideProjectServer(ctx, options), /Pi project trust/);
    assert.equal(confirmations, 0);
    trusted = true;
    assert.equal(await decideProjectServer(ctx, options), false);
    assert.equal(existsSync(ledger), false);
    assert.equal(prompt.includes('literal-secret-never-display'), false);
    assert.equal(prompt.includes('fixture-command'), true);
    assert.equal(prompt.includes(await realpath(project)), true);
    assert.equal(prompt.includes('"digest"'), true);
    confirm = async () => { await writeFile(file, config(declaration('changed-command'))); return true; };
    await assert.rejects(decideProjectServer(ctx, options), /changed during approval/);
    assert.equal(existsSync(ledger), false);
    confirm = async () => true;
    assert.equal(await decideProjectServer(ctx, options), true);
    const ready = await loadEffectiveDeclarations({ ctx, agentDir, configDirName: '.pi' });
    assert.equal(ready[0].status, 'approved');
  } finally {
    await rm(root, { force: true, recursive: true });
  }
});
