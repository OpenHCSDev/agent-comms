// Caught-error continuation controls. No production fault hooks or providers.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { syncBuiltinESMExports } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const packageDir = process.env.PI_NATIVE_PACKAGE_DIR;
assert.ok(packageDir, 'provide a disposable PI_NATIVE_PACKAGE_DIR');
const { SessionManager } = await import(pathToFileURL(join(packageDir, 'dist/core/session-manager.js')));
const root = fs.mkdtempSync(join(tmpdir(), 'pr48-failure-state-'));
const inputId = '1'.repeat(32);
const user = text => ({ role: 'user', content: text, timestamp: 1 });
const assistant = { role: 'assistant', content: [{ type: 'text', text: 'answer' }],
    provider: 'fixture', model: 'fixture', api: 'fixture', stopReason: 'stop', timestamp: 2 };
function fixture() {
    const dir = fs.mkdtempSync(join(root, 'case-'));
    const manager = SessionManager.create(dir, join(dir, 'sessions'));
    const kept = manager.appendMessage({ ...user('task'), inputId, inputDigest: '2'.repeat(64) });
    manager.appendMessage(assistant);
    const file = manager.getSessionFile();
    const witness = manager.captureCompactionWitness(kept);
    const summary = 'summary';
    const commit = { commitId: '3'.repeat(32), payloadDigest:
        createHash('sha256').update(JSON.stringify([summary, kept, 42])).digest('hex') };
    return { dir, manager, kept, file, witness, summary, commit, entries: manager.getEntries() };
}
function inject(method, replacement, action) {
    const original = fs[method];
    fs[method] = (...args) => replacement(original, ...args);
    syncBuiltinESMExports();
    try { return action(); } finally { fs[method] = original; syncBuiltinESMExports(); }
}
function identity(path) {
    const stat = fs.statSync(path, { bigint: true });
    // Reads may update atime; identity, permissions and mutation timestamps must not change.
    return ['dev', 'ino', 'mode', 'uid', 'gid', 'nlink', 'size', 'mtimeNs', 'ctimeNs'].map(key => stat[key].toString());
}
function disk(dir, metadata = false) {
    const records = metadata ? [['.', identity(dir)]] : [];
    for (const entry of fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
        const path = join(dir, entry.name);
        records.push([entry.name, entry.isDirectory() ? disk(path, metadata) : fs.readFileSync(path).toString('hex'),
            metadata ? identity(path) : null]);
    }
    return records;
}
function state(manager) {
    return JSON.stringify(Object.entries(manager).map(([key, value]) =>
        [key, value instanceof Map ? [...value] : value]));
}
function continuations(f) {
    const m = f.manager;
    return {
        appendMessage: () => m.appendMessage(user('must not append after failure')),
        appendThinkingLevelChange: () => m.appendThinkingLevelChange('off'),
        appendModelChange: () => m.appendModelChange('fixture', 'fixture'),
        appendCompaction: () => m.appendCompaction('summary', f.kept, 42),
        appendCompactionIfCurrent: () => m.appendCompactionIfCurrent(f.witness, f.summary, 42,
            { agentCommsCommit: f.commit }),
        appendCustomEntry: () => m.appendCustomEntry('fixture', {}),
        appendSessionInfo: () => m.appendSessionInfo('name'),
        appendCustomMessageEntry: () => m.appendCustomMessageEntry('fixture', 'data', true, {}),
        appendLabelChange: () => m.appendLabelChange(f.kept, 'label'),
        branch: () => m.branch(f.kept),
        resetLeaf: () => m.resetLeaf(),
        branchWithSummary: () => m.branchWithSummary(f.kept, 'summary'),
        createBranchedSession: () => m.createBranchedSession(f.kept),
        setSessionFile: () => m.setSessionFile(f.file),
        newSession: () => m.newSession(),
        flushInputDurably: () => m.flushInputDurably(inputId),
        reconcileCompactionCommit: () => m.reconcileCompactionCommit(f.commit, f.witness),
        _setSessionFile: () => m._setSessionFile(f.file),
        _loadEntries: () => m._loadEntries(f.entries),
        _buildIndex: () => m._buildIndex(),
        _rewriteFile: () => m._rewriteFile(),
        _persist: () => m._persist(f.entries[0]),
        _appendEntry: () => m._appendEntry(f.entries[0]),
        _assertLoadedRevision: () => m._assertLoadedRevision(),
        captureCompactionWitness: () => m.captureCompactionWitness(f.kept),
        assertNativeInputSafe: () => m.assertNativeInputSafe(),
        getTrackedInput: () => m.getTrackedInput(inputId),
        getSessionName: () => m.getSessionName(),
        getLeafEntry: () => m.getLeafEntry(),
        getEntry: () => m.getEntry(f.kept),
        getChildren: () => m.getChildren(f.kept),
        getLabel: () => m.getLabel(f.kept),
        getBranch: () => m.getBranch(),
        buildContextEntries: () => m.buildContextEntries(),
        buildSessionContext: () => m.buildSessionContext(),
        getHeader: () => m.getHeader(),
        getEntries: () => m.getEntries(),
        getTree: () => m.getTree(),
    };
}
const scenarios = {
    'branch-destination-lock': f => {
        let target;
        inject('openSync', (original, path, ...args) => {
            if (typeof path === 'string' && path.endsWith('.jsonl.pr48-writer.lock') &&
                path !== `${f.file}.pr48-writer.lock`) {
                target = path.slice(0, -'.pr48-writer.lock'.length);
                throw Object.assign(new Error('injected destination contention'), { code: 'EEXIST' });
            }
            return original(path, ...args);
        }, () => assert.throws(() => f.manager.createBranchedSession(f.manager.getLeafId()), /lock unavailable/));
        assert.ok(target);
        assert.equal(fs.existsSync(target), false);
    },
    'switch-malformed': f => {
        const target = join(f.dir, 'malformed.jsonl');
        fs.writeFileSync(target, fs.readFileSync(f.file));
        fs.appendFileSync(target, '{"bad":"tail"');
        assert.throws(() => f.manager.setSessionFile(target), /Incomplete/);
    },
    'switch-direct-malformed': f => {
        const target = join(f.dir, 'malformed-direct.jsonl');
        fs.writeFileSync(target, '{"bad":"tail"');
        assert.throws(() => f.manager._setSessionFile(target), /Incomplete/);
    },
    'branch-summary-lock': f => {
        const lock = `${f.file}.pr48-writer.lock`;
        fs.writeFileSync(lock, 'fixture-held\n');
        try { assert.throws(() => f.manager.branchWithSummary(f.kept, 'summary'), /lock unavailable/); }
        finally { fs.unlinkSync(lock); }
    },
    'new-session-invalid-id': f => {
        assert.throws(() => f.manager.newSession({ id: 'invalid/id' }));
    },
    'reconcile-lock': f => {
        const lock = `${f.file}.pr48-writer.lock`;
        fs.writeFileSync(lock, 'fixture-held\n');
        try { assert.throws(() => f.manager.reconcileCompactionCommit(f.commit, f.witness), /lock unavailable/); }
        finally { fs.unlinkSync(lock); }
    },
    'stale-revision': f => {
        SessionManager.open(f.file).appendMessage(user('other writer'));
        assert.throws(() => f.manager._assertLoadedRevision(), /changed during load/);
    },
    'branch-partial-write': f => {
        inject('writeFileSync', (original, fd, data, ...args) => {
            const path = typeof fd === 'number' ? fs.readlinkSync(`/proc/self/fd/${fd}`) : String(fd);
            if (path.endsWith('.jsonl') && path !== f.file) {
                original(fd, String(data).slice(0, 10), ...args);
                throw new Error('injected branch partial write');
            }
            return original(fd, data, ...args);
        }, () => assert.throws(() => f.manager.createBranchedSession(f.manager.getLeafId()), /rewrite outcome unknown/));
    },
    'branch-directory-sync': f => {
        inject('fsyncSync', (original, fd) => {
            if (fs.fstatSync(fd).isDirectory()) throw new Error('injected directory sync');
            return original(fd);
        }, () => assert.throws(() => f.manager.createBranchedSession(f.manager.getLeafId()), /rewrite outcome unknown/));
    },
    'switch-index-failure': f => {
        const target = join(f.dir, 'valid-target.jsonl');
        fs.writeFileSync(target, fs.readFileSync(f.file));
        const build = SessionManager.prototype._buildIndex;
        SessionManager.prototype._buildIndex = function () {
            this.byId.clear();
            throw new Error('injected index rebuild');
        };
        try { assert.throws(() => f.manager.setSessionFile(target), /injected index/); }
        finally { SessionManager.prototype._buildIndex = build; }
    },
    'append-partial-write': f => {
        inject('appendFileSync', (original, path, data, ...args) => {
            if (path === f.file) {
                original(path, String(data).slice(0, 10), ...args);
                throw new Error('injected append partial write');
            }
            return original(path, data, ...args);
        }, () => assert.throws(() => f.manager.appendMessage(user('uncertain')), /write outcome unknown/));
    },
    'input-sync-failure': f => {
        inject('fsyncSync', (original, fd) => {
            if (fs.readlinkSync(`/proc/self/fd/${fd}`) === f.file) throw new Error('injected input sync');
            return original(fd);
        }, () => assert.throws(() => f.manager.flushInputDurably(inputId), /injected input sync/));
    },
    'commit-sync-failure': f => {
        inject('fsyncSync', (original, fd) => {
            if (fs.readlinkSync(`/proc/self/fd/${fd}`) === f.file) throw new Error('injected commit sync');
            return original(fd);
        }, () => assert.throws(() => f.manager.appendCompactionIfCurrent(f.witness, f.summary, 42,
            { agentCommsCommit: f.commit }), /commit outcome unknown/));
    },
};
const selected = process.argv[2] ? [process.argv[2]] : Object.keys(scenarios);
let controls = 0;
for (const name of selected) {
    assert.ok(scenarios[name], `unknown scenario ${name}`);
    const f = fixture();
    scenarios[name](f);
    f.manager._acUnusable = false; // A public lookalike cannot clear the private latch.
    const beforeDisk = disk(f.dir, true), beforeBytes = disk(f.dir), beforeState = state(f.manager);
    for (const [method, action] of Object.entries(continuations(f))) {
        assert.throws(action, /manager unusable after failed mutation/, `${name}: ${method}`);
        assert.equal(state(f.manager), beforeState, `${name}: ${method} changed memory`);
        assert.deepEqual(disk(f.dir, true), beforeDisk, `${name}: ${method} changed disk`);
        controls++;
    }
    assert.equal(f.manager.nativeInputProofAvailable(), false);
    if (name === 'branch-destination-lock' || name === 'switch-malformed') {
        // Explicit fresh validated instance, not reuse/reset or replay of the failed operation.
        const fresh = SessionManager.open(f.file);
        fresh.appendMessage(user('new explicitly authorized input'));
        assert.equal(fresh.getEntries().at(-1).message.content, 'new explicitly authorized input');
        assert.throws(() => f.manager.newSession(), /manager unusable/);
    }
    if (name === 'commit-sync-failure') {
        const fresh = SessionManager.open(f.file);
        assert.equal(fresh.reconcileCompactionCommit(f.commit, f.witness).status, 'committed');
        assert.equal(fresh.getEntries().filter(e => e.details?.agentCommsCommit?.commitId === f.commit.commitId).length, 1);
        assert.deepEqual(disk(f.dir), beforeBytes, 'reconciliation is not a resend');
    }
}
console.log(JSON.stringify({ ok: true, scenarios: selected, continuationControls: controls, root }));
