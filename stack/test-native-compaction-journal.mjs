// Provider-free real native commit/reconciliation contracts; disposable package only.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';

const packageDir = process.env.PI_NATIVE_PACKAGE_DIR;
assert.ok(packageDir, 'provide disposable PI_NATIVE_PACKAGE_DIR');
const { SessionManager } = await import(pathToFileURL(join(packageDir, 'dist/core/session-manager.js')));
const root = mkdtempSync(join(tmpdir(), 'pr48-journal-'));
function fixture() {
    const manager = SessionManager.create(root, join(root, 'sessions'));
    const kept = manager.appendMessage({ role: 'user', content: 'task', timestamp: 1 });
    manager.appendMessage({ role: 'assistant', content: [{ type: 'text', text: 'answer' }],
        provider: 'fixture', model: 'fixture', api: 'fixture', stopReason: 'stop', timestamp: 2 });
    const witness = manager.captureCompactionWitness(kept);
    const summary = 'summary';
    const commit = { commitId: createHash('md5').update(witness.sessionId).digest('hex'),
        payloadDigest: createHash('sha256').update(JSON.stringify([summary, kept, 42])).digest('hex') };
    return { manager, witness, summary, commit };
}
function append(f) {
    return f.manager.appendCompactionIfCurrent(f.witness, f.summary, 42,
        { agentCommsCommit: f.commit });
}
{
    const f = fixture();
    assert.equal(f.manager.reconcileCompactionCommit(f.commit, f.witness).status, 'aborted-no-write');
    const entryId = append(f);
    const reopened = SessionManager.open(f.witness.sessionFile);
    const result = reopened.reconcileCompactionCommit(f.commit, f.witness);
    assert.equal(result.status, 'committed');
    assert.equal(result.entryId, entryId);
    assert.equal(result.leafId, entryId);
    // A new witness may not grant a second mutation with the same operation ID.
    const refreshed = reopened.captureCompactionWitness(f.witness.firstKeptEntryId);
    assert.throws(() => reopened.appendCompactionIfCurrent(refreshed, f.summary, 42,
        { agentCommsCommit: f.commit }), /already present/);
    reopened.appendMessage({ role: 'user', content: 'later', timestamp: 3 });
    assert.equal(reopened.reconcileCompactionCommit(f.commit, f.witness).entryId, entryId);
}
{
    const f = fixture();
    f.manager.appendMessage({ role: 'user', content: 'other writer', timestamp: 3 });
    assert.equal(f.manager.reconcileCompactionCommit(f.commit, f.witness).status, 'unknown');
    assert.throws(() => append(f), /Invalid native compaction witness/);
}
{
    const f = fixture();
    const before = readFileSync(f.witness.sessionFile);
    f.commit.payloadDigest = '0'.repeat(64);
    assert.throws(() => append(f), /payload mismatch/);
    assert.deepEqual(readFileSync(f.witness.sessionFile), before);
}
{
    const f = fixture();
    append(f);
    const lines = readFileSync(f.witness.sessionFile, 'utf8').trimEnd().split('\n').map(JSON.parse);
    lines.at(-1).summary = 'tampered';
    writeFileSync(f.witness.sessionFile, lines.map(JSON.stringify).join('\n') + '\n');
    assert.equal(f.manager.reconcileCompactionCommit(f.commit, f.witness).status, 'unknown');
}
{
    const f = fixture();
    append(f);
    const lines = readFileSync(f.witness.sessionFile, 'utf8').trimEnd().split('\n').map(JSON.parse);
    lines.push({ ...lines.at(-1), id: 'duplicated-operation' });
    writeFileSync(f.witness.sessionFile, lines.map(JSON.stringify).join('\n') + '\n');
    assert.equal(f.manager.reconcileCompactionCommit(f.commit, f.witness).status, 'unknown');
}
{
    const f = fixture();
    writeFileSync(f.witness.sessionFile, '{"incomplete":', { flag: 'a' });
    assert.throws(() => f.manager.reconcileCompactionCommit(f.commit, f.witness), /Incomplete/);
}
{
    const f = fixture();
    // Explicit operator removal only removes the lock. It is not proof that
    // an unknown compaction was absent, committed, or eligible for retry.
    append(f);
    const lock = `${f.witness.sessionFile}.pr48-writer.lock`;
    writeFileSync(lock, 'operator-verified-dead-holder\n', { flag: 'wx' });
    assert.throws(() => f.manager.reconcileCompactionCommit(f.commit, f.witness), /lock unavailable/);
    unlinkSync(lock);
    assert.equal(f.manager.reconcileCompactionCommit(f.commit, f.witness).status, 'committed');
}
console.log(JSON.stringify({ ok: true, cases: 7, root }));
