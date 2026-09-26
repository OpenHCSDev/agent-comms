// Blocker-3 contract: a single-writer rule backed by the shared per-session lock.
// Four concurrent processes race to append to one native session; exactly one
// may commit a prefix of its burst, other writers refuse BEFORE mutation, and the
// resulting file must stay valid JSONL with no torn or interleaved lines.
// Provider-free and runtime-dormant; requires the disposable prototype package.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const packageDir = process.env.PI_NATIVE_PACKAGE_DIR;
if (!packageDir) throw new Error('Explicit disposable PI_NATIVE_PACKAGE_DIR required');
const probePath = new URL('./probe-native-adaptive-writer.mjs', import.meta.url).pathname;
const { SessionManager } = await import(pathToFileURL(join(packageDir, 'dist/index.js')).href);

const root = mkdtempSync(join(tmpdir(), 'pr48-writer-exclusivity-'));
try {
  const manager = SessionManager.create(root, join(root, 'sessions'));
  manager.appendMessage({
    role: 'user', content: [{ type: 'text', text: 'seed' }], timestamp: Date.now(),
  });
  // Force a durable file so concurrent openers have bytes to load and race on.
  manager.appendMessage({
    role: 'assistant', content: [{ type: 'text', text: 'seed answer' }],
    provider: 'fixture', model: 'fixture', api: 'fixture', stopReason: 'stop',
    timestamp: Date.now(),
  });
  const sessionFile = manager.getSessionFile();

  const gate = join(root, 'burst-gate');
  const children = Array.from({ length: 4 }, (_, index) => new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [probePath, 'append-burst', sessionFile], {
      env: { ...process.env, PI_NATIVE_PACKAGE_DIR: packageDir, PR48_BURST_GATE: gate },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.on('error', reject);
    child.on('close', code => {
      if (code !== 0) reject(new Error(`child ${index} exited ${code}`));
      else resolve(JSON.parse(stdout.trim()));
    });
    void index;
  }));
  // Wait until all four children have OPENED the session (recorded their
  // loaded revision), then release the gate: every first append collides.
  const { readdirSync } = await import('node:fs');
  const deadline = Date.now() + 15000;
  while (readdirSync(root).filter(name => name.includes('.open-')).length < 4) {
    if (Date.now() > deadline) throw new Error('children never opened the session');
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  writeFileSync(gate, 'go');
  const outcomes = await Promise.all(children);

  const winners = outcomes.filter(outcome => outcome.appended > 0);
  const losers = outcomes.filter(outcome => outcome.appended === 0);
  // Blocker-3 invariant: the shared lock plus load-time revision check admits
  // exactly one aligned writer; every other aligned writer refuses entirely.
  assert.equal(winners.length, 1, `exactly one writer may commit: ${JSON.stringify(outcomes)}`);
  assert.ok(winners[0].appended >= 1 && winners[0].appended <= 3,
    `winner may retire on later contention, never retry: ${JSON.stringify(outcomes)}`);
  assert.equal(losers.length, outcomes.length - 1);
  assert.ok(losers.every(outcome => outcome.refusals.length > 0), 'losers must refuse, not hang');
  assert.ok(
    losers.every(outcome =>
      outcome.refusals.some(text =>
        text.includes('Native session writer changed') ||
        text.includes('Native session writer lock unavailable'))),
    'loser refusals must be typed fail-closed denials',
  );

  console.log('outcomes', JSON.stringify(outcomes));
  const lines = readFileSync(sessionFile, 'utf8').split('\n').filter(Boolean);
  const entries = lines.map(line => JSON.parse(line)); // throws on any torn line
  const header = entries.find(entry => entry.type === 'session');
  assert.ok(header, 'session header intact');
  const messages = entries.filter(entry => entry.type === 'message');
  const committed = winners[0].appended;
  // Per-append atomicity: every committed message is intact and exactly the
  // set the children report; staleness refuses, never silent interleaving.
  // Two seed messages (user + assistant) plus every reported burst append.
  assert.equal(messages.length, 2 + committed);
  assert.equal(new Set(entries.map(entry => entry.id)).size, entries.length, 'unique entry IDs');
  const parents = new Set(entries.map(entry => entry.id));
  assert.ok(messages.every(entry => entry.parentId === null || parents.has(entry.parentId)),
    'every parent link resolves within the file');

  console.log(JSON.stringify({ outcomes: outcomes.map(o => ({ pid: o.pid, appended: o.appended })),
    validJsonlLines: lines.length, committed }));
  console.log('native writer exclusivity contract PASS');
} finally {
  rmSync(root, { recursive: true, force: true });
}
