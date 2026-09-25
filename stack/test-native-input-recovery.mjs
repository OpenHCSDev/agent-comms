// Offline startup proof validation, including bounded work for large histories.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';

const manifest = readFileSync(resolve(import.meta.dirname, 'pi-native.sha256'));
const buildId = createHash('sha256').update(manifest).digest('hex').slice(0, 16);
const packageDir = process.env.PI_NATIVE_PACKAGE_DIR ?? resolve(
  import.meta.dirname, `.pi-native-${buildId}/node_modules/@earendil-works/pi-coding-agent`,
);
const { AgentSession, SessionManager } = await import(pathToFileURL(join(packageDir, 'dist/index.js')).href);
const inputId = (n) => n.toString(16).padStart(32, '0');
const digest = 'a'.repeat(64);
const entry = (n) => ({
  type: 'message', id: `entry-${n}`, parentId: null,
  message: { role: 'user', inputId: inputId(n), inputDigest: digest },
});
const row = (n, generation = 1) => ({
  schema: 1, type: 'context_committed', sessionId: 'session',
  inputId: inputId(n), sessionEntryId: `entry-${n}`,
  requestGeneration: generation, llmContextDigest: digest,
});

function recover(entries, rows) {
  const root = mkdtempSync(join(tmpdir(), 'native-input-recovery-'));
  try {
    const proof = join(root, 'proof');
    writeFileSync(proof, rows.map((value) => JSON.stringify(value) + '\n').join(''));
    const manager = Object.create(SessionManager.prototype);
    manager.fileEntries = entries;
    manager.isPersisted = () => true;
    manager.assertNativeInputSafe = () => true;
    const state = {
      sessionManager: manager, sessionId: 'session',
      _nativeInputClaims: new Map(), _nativeRequestGeneration: 0,
      _nativeProofPath: () => proof,
      _emit() { assert.fail('recovery must never emit a live receipt'); },
    };
    AgentSession.prototype._loadNativeInputState.call(state);
    return state;
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

test('large branched histories and repeated generations require linear entry visits', () => {
  let visits = 0;
  const entries = Array.from({ length: 20_000 }, (_, n) => {
    const value = n < 300 ? entry(n) : { type: 'message', message: { role: 'assistant' } };
    return new Proxy(value, { get(target, key) { if (key === 'type') visits++; return target[key]; } });
  });
  const rows = Array.from({ length: 10 }, (_, generation) =>
    Array.from({ length: 300 }, (_, n) => row(n, generation + 1))).flat();
  const state = recover(entries, rows);
  assert.equal(state._nativeInputClaims.size, 300);
  assert.equal(state._nativeRequestGeneration, 10);
  assert.ok(visits <= entries.length * 2, `visited ${visits} entries`);
});

for (const [name, damage] of [
  ['duplicate tracked input', (entries) => entries.push(entry(1))],
  ['invalid input ID', (entries) => entries[0].message.inputId = 'bad'],
  ['invalid request digest', (entries) => entries[0].message.inputDigest = 'bad'],
  ['missing input', (_entries, rows) => rows[0].inputId = inputId(9)],
  ['wrong entry ID', (_entries, rows) => rows[0].sessionEntryId = 'wrong'],
  ['wrong session', (_entries, rows) => rows[0].sessionId = 'wrong'],
  ['duplicate input within generation', (_entries, rows) => rows.push(row(1))],
  ['regressing generation', (_entries, rows) => rows.push(row(1, 2), row(1, 1))],
  ['inconsistent context digest', (_entries, rows) => rows[1].llmContextDigest = 'b'.repeat(64)],
  ['invalid generation', (_entries, rows) => rows[0].requestGeneration = 0],
]) {
  test(`recovery rejects ${name}`, () => {
    const entries = [entry(1), entry(2)];
    const rows = [row(1), row(2)];
    damage(entries, rows);
    assert.throws(() => recover(entries, rows), /Invalid or duplicate native input|Invalid native input proof journal/);
  });
}
