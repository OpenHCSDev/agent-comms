// Offline AgentSession journal boundary. Files live under the caller's owned TMPDIR.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { appendFileSync, closeSync, mkdtempSync, openSync, rmSync, statSync, writeSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';

const packageDir = process.env.PI_NATIVE_PACKAGE_DIR;
assert(packageDir, 'PI_NATIVE_PACKAGE_DIR must identify the candidate package');
const { AgentSession } = await import(pathToFileURL(resolve(packageDir, 'dist/core/agent-session.js')));
const request = { text: 'original' };
const inputId = '1'.repeat(32), contextDigest = 'b'.repeat(64);
const inputDigest = createHash('sha256').update('pi-input-request-v1\n' + JSON.stringify(request)).digest('hex');
const entry = { id: 'entry', message: { inputId, inputDigest } };
const row = (requestGeneration = 1) => ({ schema: 1, type: 'context_committed', sessionId: 'session',
  inputId, sessionEntryId: entry.id, requestGeneration, llmContextDigest: contextDigest });

function fixture(run) {
  const root = mkdtempSync(join(tmpdir(), 'native-proof-'));
  const file = join(root, 'proof');
  const state = Object.assign(Object.create(AgentSession.prototype), {
    sessionManager: {
      entryStore: { *trackedInputs() { yield entry; } },
      getTrackedInput(id) { return id === inputId ? entry : undefined; },
      isPersisted() { return true; },
      getSessionId() { return 'session'; },
    },
    _nativeInputClaims: new Map(), _nativeRequestGeneration: 0,
    _nativeProofPath: () => file,
    _emit() { assert.fail('recovery must never emit acceptance'); },
  });
  try { return run(file, state); }
  finally { rmSync(root, { recursive: true, force: true }); }
}

test('retained proof larger than retired cap streams with no claim/body mirror or replay', () => fixture((file, state) => {
  const fd = openSync(file, 'wx', 0o600);
  let generation = 0, bytes = 0;
  try {
    while (bytes <= 256 * 1024 * 1024) {
      let batch = '';
      for (let i = 0; i < 2048; i++) batch += JSON.stringify(row(++generation)) + '\n';
      bytes += writeSync(fd, batch);
    }
  } finally { closeSync(fd); }
  state._loadNativeInputState();
  assert.equal(state._nativeRequestGeneration, generation);
  assert.equal(state._nativeInputClaims.size, 0);
  assert.equal(statSync(file).size, bytes);
  // A committed historical claim remains authoritative even without a live map entry.
  assert.equal(state._claimNativeInput(inputId, request), true);
  assert.throws(() => state._claimNativeInput(inputId, { text: 'different' }), /Conflicting replay/);
  assert.equal(state.sessionManager.getTrackedInput(inputId).message.inputDigest, inputDigest);
  const rss = process.resourceUsage().maxRSS * 1024;
  assert(rss < bytes, `peak RSS ${rss} must remain below journal bytes ${bytes}`);
  console.log(JSON.stringify({ journalBytes: bytes, generations: generation, peakRSS: rss, recoveryEmissions: 0 }));
}));

test('historical UNKNOWN without a proof row still prevents input replay', () => fixture((file, state) => {
  // Durable native entry exists, but no journal fsync/live acceptance is proved.
  state._loadNativeInputState();
  assert.equal(state._nativeRequestGeneration, 0);
  assert.equal(state._claimNativeInput(inputId, request), true);
  assert.throws(() => state._claimNativeInput(inputId, {text: 'different'}), /Conflicting replay/);
  assert.equal(state._nativeInputClaims.size, 0);
}));

test('live context commit releases only pending memory and preserves later digest binding', async () => {
  const root = mkdtempSync(join(tmpdir(), 'native-proof-live-'));
  const file = join(root, 'session.input-proof'), receipts = [];
  const state = Object.assign(Object.create(AgentSession.prototype), {
    sessionManager: {
      getTrackedInput(id) { return id === inputId ? entry : undefined; },
      flushInputDurably(id) { assert.equal(id, inputId); return entry.id; },
      getSessionId() { return 'session'; },
    },
    _nativeInputClaims: new Map([[inputId, inputDigest]]),
    _awaitingNativeInputIds: new Set([inputId]), _nativeRequestGeneration: 0,
    _nativeProofPath: () => file, _emit: event => receipts.push(event),
  });
  try {
    const context = {messages: [{role: 'user', inputId, inputDigest}]};
    await state._commitNativeContext(context);
    assert.equal(state._nativeInputClaims.size, 0);
    await state._commitNativeContext(context);
    assert.deepEqual(receipts.map(row => row.requestGeneration), [1, 2]);
    assert.equal([...state._nativeProofRows(file)].length, 2);
    assert.equal(state._claimNativeInput(inputId, request), true);
    await assert.rejects(() => state._commitNativeContext({messages: [
      {...context.messages[0], inputDigest: '0'.repeat(64)}]}), /unbound native input/);
  } finally { rmSync(root, {recursive: true, force: true}); }
});

test('malformed, incomplete, changed or inconsistent historical proof never becomes acceptance', () => {
  const valid = row();
  for (const raw of [
    JSON.stringify(valid),
    Buffer.from([0xff, 10]),
    '{bad}\n',
    JSON.stringify({ ...valid, inputId: '0'.repeat(32) }) + '\n',
    JSON.stringify({ ...valid, sessionEntryId: 'wrong' }) + '\n',
    JSON.stringify({ ...valid, sessionId: 'wrong' }) + '\n',
    JSON.stringify(valid) + '\n' + JSON.stringify(valid) + '\n',
    JSON.stringify(row(2)) + '\n' + JSON.stringify(valid) + '\n',
  ]) fixture((file, state) => {
    writeFileSync(file, raw);
    assert.throws(() => state._loadNativeInputState());
    assert.equal(state._nativeInputClaims.size, 0);
  });
  fixture((file, state) => {
    writeFileSync(file, JSON.stringify(valid) + '\n');
    const rows = state._nativeProofRows(file);
    assert.deepEqual(rows.next().value, valid);
    appendFileSync(file, '\n');
    assert.throws(() => rows.next(), /changed during recovery/);
  });
});
