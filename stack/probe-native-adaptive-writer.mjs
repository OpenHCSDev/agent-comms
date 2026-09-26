// Opt-in, provider-free negative probe for pinned native SessionManager only.
// This is NOT adaptive runtime wiring, a provider transport, or an owner proof.
import assert from 'node:assert/strict';
import { createInterface } from 'node:readline';
import { closeSync, fsyncSync, openSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [mode, target] = process.argv.slice(2);
const packageDir = process.env.PI_NATIVE_PACKAGE_DIR;
if (!packageDir ||
    !['pending', 'external-append', 'hold-lock', 'append-burst', 'operator-recover']
      .includes(mode) || !target)
  throw new Error('Provide PI_NATIVE_PACKAGE_DIR and pending <root> or a mode with <session-file>');
const { SessionManager } = await import(pathToFileURL(join(packageDir, 'dist/index.js')).href);
const message = text => ({ role: 'user', content: [{ type: 'text', text }], timestamp: Date.now() });

if (mode === 'hold-lock') {
  const fd = openSync(`${target}.pr48-writer.lock`, 'wx', 0o600);
  writeFileSync(fd, `${process.pid}\n`);
  fsyncSync(fd);
  closeSync(fd);
  console.log(JSON.stringify({ phase: 'lock-held' }));
  await new Promise(resolve => setTimeout(resolve, 3600000)); // Fixture kills; lock persists.
} else if (mode === 'append-burst') {
  // Blocker-3 fixture: many processes append concurrently; every append must
  // either commit under the shared lock or refuse before mutation.
  const gate = process.env.PR48_BURST_GATE;
  const writer = SessionManager.open(target);
  if (gate) {
    // Open the session first (records the loaded revision), report readiness,
    // then hold appends until the go gate aligns every writer's first append.
    const { writeFileSync: signalReady, existsSync } = await import('node:fs');
    signalReady(`${gate}.open-${process.pid}`, 'open');
    while (!existsSync(gate)) await new Promise(resolve => setTimeout(resolve, 1));
  }
  let appended = 0;
  const refusals = [];
  for (let index = 0; index < 3; index += 1) {
    // Any failed mutator retires this manager, including pre-write contention.
    // Never retry on the same object or silently construct a replacement here.
    try {
      writer.appendMessage(message(`burst ${process.pid} ${index}`));
      appended += 1;
    } catch (error) {
      refusals.push(String(error));
    }
    if (refusals.length > 0) break;
  }
  console.log(JSON.stringify({ pid: process.pid, appended, refusals }));
} else if (mode === 'operator-recover') {
  // Blocker-4 fixture: after a crash leaves the lock, the ONLY sanctioned
  // recovery is an explicit operator action on the lock file, verified
  // against the disk revision before any further append.
  const lockPath = `${target}.pr48-writer.lock`;
  const { existsSync, unlinkSync } = await import('node:fs');
  assert.ok(existsSync(lockPath), 'operator recovery requires a stale lock');
  unlinkSync(lockPath); // Operator decision; never automated by the writer.
  const writer = SessionManager.open(target);
  const id = writer.appendMessage(message('operator-recovered append'));
  console.log(JSON.stringify({ phase: 'recovered', appendedId: id,
    leaf: writer.getLeafId() }));
} else if (mode === 'external-append') {
  const writer = SessionManager.open(target);
  const id = writer.appendMessage(message('independent writer after preflight'));
  console.log(JSON.stringify({ appendedId: id, leaf: writer.getLeafId() }));
} else {
  const manager = SessionManager.create(target, join(target, 'sessions'));
  const firstKeptEntryId = manager.appendMessage(message('original task'));
  manager.appendMessage({
    role: 'assistant', content: [{ type: 'text', text: 'first answer' }],
    provider: 'fixture', model: 'fixture', api: 'fixture', stopReason: 'stop',
    timestamp: Date.now(),
  });
  const capturedLeaf = manager.getLeafId();
  const capturedBranch = manager.getBranch();
  const sessionFile = manager.getSessionFile();
  if (!sessionFile) throw new Error('A persisted native session file is required');
  const witness = manager.captureCompactionWitness?.(firstKeptEntryId);
  console.log(JSON.stringify({ phase: 'captured', capturedLeaf, sessionFile,
    branchLength: capturedBranch.length, firstKeptEntryId, guarded: Boolean(witness),
    sessionRevision: witness ? witness.revision : null }));
  const readline = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const lines = [];
  for await (const line of readline) {
    lines.push(line);
    if (lines.length === 2) break;
  }
  readline.close();
  const [action, attestationLine] = lines;
  let attestation;
  if (attestationLine !== undefined) {
    try { attestation = JSON.parse(attestationLine); }
    catch { attestation = { malformed: true }; }
  }
  if (action === 'append-local') manager.appendMessage(message('later local input'));
  else if (action !== 'continue' && action !== 'continue-owner-goal' &&
      action !== 'probe-unknown-write') throw new Error('Unexpected barrier action');
  const leafBeforeCommit = manager.getLeafId();
  let commitId = null;
  let commitError = null;
  try {
    // Stock Pi has no CAS. The isolated prototype requires an explicit native
    // witness plus, for owner-scoped work, a Python-issued attestation whose
    // fence must match this witness; Python still rechecks at commit time.
    commitId = witness
      ? manager.appendCompactionIfCurrent(witness, 'STALE CANDIDATE', 42,
          undefined, undefined,
          action === 'continue-owner-goal'
            ? { ownerRequired: true, attestation }
            : {})
      : manager.appendCompaction('STALE CANDIDATE', firstKeptEntryId, 42);
  } catch (error) {
    commitError = String(error);
  }
  let subsequentError = null;
  if (action === 'probe-unknown-write') {
    try { manager.appendMessage(message('must not replay after uncertain commit')); }
    catch (error) { subsequentError = String(error); }
  }
  console.log(JSON.stringify({ phase: 'committed', capturedLeaf, leafBeforeCommit,
    leafAfterCommit: manager.getLeafId(), commitId, commitError, subsequentError,
    sessionFile, nativeBranchTail: manager.getBranch().at(-1)?.type }));
}
