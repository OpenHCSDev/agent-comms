// Provider-free adversarial loader/fork/snapshot tests; disposable package only.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const packageDir = process.env.PI_NATIVE_PACKAGE_DIR;
assert.ok(packageDir, 'provide disposable PI_NATIVE_PACKAGE_DIR');
const moduleURL = pathToFileURL(join(packageDir, 'dist/core/session-manager.js')).href;
const { SessionManager } = await import(moduleURL);
const root = fs.mkdtempSync(join(tmpdir(), 'pr48-writer-coverage-'));
process.env.AGENT_COMMS_SESSION_INDEX_DIR = join(root, 'indexes');
const managers = new Set();
function opened(file) {
    const manager = SessionManager.open(file);
    managers.add(manager);
    return manager;
}
const user = content => ({ role: 'user', content, timestamp: 1 });
const assistant = { role: 'assistant', content: [{ type: 'text', text: 'answer' }],
    provider: 'fixture', model: 'fixture', api: 'fixture', stopReason: 'stop', timestamp: 2 };
function fixture() {
    const manager = SessionManager.create(root, join(root, 'sessions'));
    managers.add(manager);
    const kept = manager.appendMessage(user('task'));
    manager.appendMessage(assistant);
    return { manager, file: manager.getSessionFile(), kept };
}
function rows(file) { return fs.readFileSync(file, 'utf8').trimEnd().split('\n').map(JSON.parse); }
function externalAppend(file, initialize = false) {
    const code = `const {SessionManager} = await import(${JSON.stringify(moduleURL)});
        const manager = SessionManager.open(process.argv[1]);
        manager.appendMessage(${JSON.stringify(user('external'))});
        if (process.argv[2] === 'initialize') manager.appendMessage(${JSON.stringify(assistant)});`;
    const result = spawnSync(process.execPath, ['--input-type=module', '-e', code, file,
        initialize ? 'initialize' : 'append'], { encoding: 'utf8', timeout: 5000 });
    assert.equal(result.status, 0, result.stderr);
}
function duringRead(file, action) {
    const original = fs.readSync;
    let armed = true;
    fs.readSync = (fd, ...args) => {
        const result = original(fd, ...args);
        if (armed && fs.readlinkSync(`/proc/self/fd/${fd}`) === file) {
            armed = false;
            externalAppend(file);
        }
        return result;
    };
    syncBuiltinESMExports();
    try { action(); assert.equal(armed, false, 'real native file read must occur'); }
    finally { fs.readSync = original; syncBuiltinESMExports(); }
}
function fixedTime(action) {
    const OriginalDate = Date;
    globalThis.Date = class extends OriginalDate {
        constructor(...args) { super(...(args.length ? args : ['2026-01-01T00:00:00.000Z'])); }
    };
    try { return action(); } finally { globalThis.Date = OriginalDate; }
}
const cases = {
    'duplicate-entry-id': () => {
        const { file } = fixture();
        const original = fs.readFileSync(file);
        fs.appendFileSync(file, JSON.stringify(rows(file)[1]) + '\n');
        const invalid = fs.readFileSync(file);
        assert.throws(() => opened(file), /Invalid|duplicate/i);
        assert.deepEqual(fs.readFileSync(file), invalid);
        assert.deepEqual(invalid.subarray(0, original.length), original);
    },
    'forward-parent-id': () => {
        const { file } = fixture();
        const entries = rows(file);
        entries[1].parentId = entries[2].id;
        fs.writeFileSync(file, entries.map(JSON.stringify).join('\n') + '\n');
        const invalid = fs.readFileSync(file);
        assert.throws(() => opened(file), /ancestry/i);
        assert.deepEqual(fs.readFileSync(file), invalid);
    },
    'newline-repair': () => {
        const { file } = fixture();
        const raw = fs.readFileSync(file).subarray(0, -1);
        fs.writeFileSync(file, raw);
        fs.writeFileSync(`${file}.pr48-writer.lock`, 'held\n');
        try {
            assert.throws(() => opened(file), /Incomplete/);
            assert.deepEqual(fs.readFileSync(file), raw);
        } finally { fs.unlinkSync(`${file}.pr48-writer.lock`); }
    },
    'malformed-load': () => {
        const { file } = fixture();
        fs.appendFileSync(file, '{broken}\n');
        const raw = fs.readFileSync(file);
        assert.throws(() => opened(file));
        assert.deepEqual(fs.readFileSync(file), raw);
    },
    'invalid-utf8-load': () => {
        const { file } = fixture();
        const raw = fs.readFileSync(file);
        raw[raw.indexOf(Buffer.from('task'))] = 0xff;
        fs.writeFileSync(file, raw);
        assert.throws(() => opened(file), /encoded data/);
        assert.deepEqual(fs.readFileSync(file), raw);
    },
    'duplicate-header-load': () => {
        const { file } = fixture();
        fs.appendFileSync(file, JSON.stringify({ ...rows(file)[0], id: 'second-header' }) + '\n');
        assert.throws(() => opened(file), /ancestry/);
    },
    'orphan-load': () => {
        const { file } = fixture();
        const entries = rows(file);
        entries.at(-1).parentId = 'missing-parent';
        fs.writeFileSync(file, entries.map(JSON.stringify).join('\n') + '\n');
        assert.throws(() => opened(file), /ancestry/);
    },
    'legacy-load': () => {
        const { file } = fixture();
        const entries = rows(file);
        entries[0].version = 2;
        fs.writeFileSync(file, entries.map(JSON.stringify).join('\n') + '\n');
        const raw = fs.readFileSync(file);
        assert.throws(() => opened(file), /explicit recovery/);
        assert.deepEqual(fs.readFileSync(file), raw);
    },
    'read-snapshot-race': () => {
        const { file } = fixture();
        const before = rows(file).length;
        duringRead(file, () => assert.throws(() => opened(file), /changed|revision/i));
        assert.equal(rows(file).length, before + 1);
        assert.equal(rows(file).at(-1).message.content, 'external');
    },
    'set-session-race': () => {
        const { file } = fixture();
        const manager = fixture().manager;
        duringRead(file, () => assert.throws(() => manager.setSessionFile(file), /changed|revision/i));
        const raw = fs.readFileSync(file);
        assert.throws(() => manager.appendMessage(user('must not attach stale sibling')), /manager unusable/);
        assert.deepEqual(fs.readFileSync(file), raw);
    },
    'empty-file-race': () => {
        const file = join(root, 'empty.jsonl');
        fs.writeFileSync(file, '', { mode: 0o600 });
        const fresh = SessionManager.prototype.newSession;
        let injected = false;
        SessionManager.prototype.newSession = function(...args) {
            if (this.sessionFile === file && !injected) {
                injected = true;
                externalAppend(file, true);
            }
            return fresh.apply(this, args);
        };
        let manager;
        try {
            try { manager = opened(file); }
            catch (error) { assert.match(String(error), /changed|revision/i); }
        }
        finally { SessionManager.prototype.newSession = fresh; }
        assert.equal(rows(file).length, 3);
        assert.equal(rows(file)[1].message.content, 'external');
        if (manager) {
            // A coherent reload is also safe; adopting the later stat while
            // retaining the first initializer's unrelated header is not.
            assert.equal(manager.getSessionId(), rows(file)[0].id,
                'returned manager must own the observed saved session');
            assert.equal(manager.getLeafId(), rows(file).at(-1).id);
            const external = fs.readFileSync(file);
            manager.appendMessage(assistant);
            assert.deepEqual(fs.readFileSync(file).subarray(0, external.length), external,
                'next flush must preserve the other writer\'s initialized session');
        }
    },
    'fork-source-lock': () => {
        const { file } = fixture();
        const target = fs.mkdtempSync(join(root, 'fork-source-'));
        fs.writeFileSync(`${file}.pr48-writer.lock`, 'held\n');
        try {
            assert.throws(() => SessionManager.forkFrom(file, root, target), /lock unavailable/);
            assert.deepEqual(fs.readdirSync(target), []);
        } finally { fs.unlinkSync(`${file}.pr48-writer.lock`); }
    },
    'fork-target-lock': () => {
        const { file } = fixture();
        const target = fs.mkdtempSync(join(root, 'fork-target-'));
        const id = '01900000-0000-7000-8000-000000000001';
        const destination = join(target, `2026-01-01T00-00-00-000Z_${id}.jsonl`);
        fs.writeFileSync(`${destination}.pr48-writer.lock`, 'held\n');
        const source = fs.readFileSync(file);
        try {
            assert.throws(() => fixedTime(() => SessionManager.forkFrom(file, root, target, { id })),
                /lock unavailable/);
            assert.equal(fs.existsSync(destination), false);
            assert.deepEqual(fs.readFileSync(file), source);
        } finally { fs.unlinkSync(`${destination}.pr48-writer.lock`); }
    },
    'fork-durable': () => {
        const { file } = fixture();
        const target = fs.mkdtempSync(join(root, 'fork-durable-'));
        const original = fs.fsyncSync;
        const synced = [];
        fs.fsyncSync = fd => {
            synced.push(fs.readlinkSync(`/proc/self/fd/${fd}`));
            original(fd);
        };
        syncBuiltinESMExports();
        let fork;
        try { fork = SessionManager.forkFrom(file, root, target); }
        finally { fs.fsyncSync = original; syncBuiltinESMExports(); }
        managers.add(fork);
        const destination = fork.getSessionFile();
        assert.equal(rows(destination).length, rows(file).length);
        assert.ok(synced.indexOf(destination) >= 0);
        assert.ok(synced.lastIndexOf(target) > synced.indexOf(destination));
        assert.ok(synced.includes('/'));
    },
    'fork-write-unknown': () => {
        const { file } = fixture();
        const target = fs.mkdtempSync(join(root, 'fork-write-unknown-'));
        const original = fs.writeFileSync;
        const source = fs.readFileSync(file);
        fs.writeFileSync = (fd, data, ...args) => {
            const destination = typeof fd === 'number' ? fs.readlinkSync(`/proc/self/fd/${fd}`) : '';
            if (destination.startsWith(target + '/') && destination.endsWith('.jsonl') &&
                String(data).includes('"role":"user"')) {
                original(fd, String(data).slice(0, 10), ...args);
                throw new Error('injected partial fork write');
            }
            return original(fd, data, ...args);
        };
        syncBuiltinESMExports();
        try { assert.throws(() => SessionManager.forkFrom(file, root, target), /fork outcome unknown/); }
        finally { fs.writeFileSync = original; syncBuiltinESMExports(); }
        const [destination] = fs.readdirSync(target);
        assert.ok(destination.endsWith('.jsonl'));
        const partial = fs.readFileSync(join(target, destination));
        assert.throws(() => opened(join(target, destination)), /Incomplete/);
        assert.deepEqual(fs.readFileSync(join(target, destination)), partial);
        assert.deepEqual(fs.readFileSync(file), source);
    },
    'fork-sync-unknown': () => {
        const { file } = fixture();
        const target = fs.mkdtempSync(join(root, 'fork-sync-unknown-'));
        const original = fs.fsyncSync;
        fs.fsyncSync = fd => {
            if (fs.readlinkSync(`/proc/self/fd/${fd}`) === target)
                throw new Error('injected fork directory durability failure');
            original(fd);
        };
        syncBuiltinESMExports();
        try { assert.throws(() => SessionManager.forkFrom(file, root, target), /fork outcome unknown/); }
        finally { fs.fsyncSync = original; syncBuiltinESMExports(); }
    },
    'branch-source-lock': () => {
        const { file, manager } = fixture();
        const leaf = manager.getLeafId();
        fs.writeFileSync(`${file}.pr48-writer.lock`, 'held\n');
        try {
            assert.throws(() => manager.createBranchedSession(leaf), /lock unavailable/);
            assert.equal(manager.getSessionFile(), file);
            assert.equal(manager.getLeafId(), leaf);
        } finally { fs.unlinkSync(`${file}.pr48-writer.lock`); }
    },
    'branch-stale-source': () => {
        const { file, manager } = fixture();
        const leaf = manager.getLeafId();
        externalAppend(file);
        assert.throws(() => manager.createBranchedSession(leaf), /changed during load/);
        assert.equal(manager.getSessionFile(), file);
    },
    'branch-positive': () => {
        const { manager, file } = fixture();
        const branch = manager.createBranchedSession(manager.getLeafId());
        assert.notEqual(branch, file);
        assert.equal(rows(branch).length, rows(file).length);
        manager.appendMessage(user('new branch'));
        assert.equal(opened(branch).getLeafId(), manager.getLeafId());
    },
};
if (process.env.AC_CAPACITY_SESSION) {
    cases['large-history-branch-replay'] = () => {
        const file = process.env.AC_CAPACITY_SESSION;
        const proof = JSON.parse(fs.readFileSync(process.env.AC_CAPACITY_FIXTURE, 'utf8'));
        const fingerprint = () => {
            const hash = createHash('sha256');
            const fd = fs.openSync(file, 'r');
            const buffer = Buffer.alloc(65536);
            try {
                let count;
                while ((count = fs.readSync(fd, buffer, 0, buffer.length, null)) > 0)
                    hash.update(buffer.subarray(0, count));
                return hash.digest('hex');
            } finally { fs.closeSync(fd); }
        };
        const before = fingerprint();
        const size = fs.statSync(file).size;
        assert.ok(size > 256 * 1024 * 1024, 'actual disk history must exceed old ceiling');
        const manager = opened(file);
        assert.equal(manager.getSessionId(), proof.session_id);
        assert.ok(manager.getEntry('history-user-0').message.content[0].text
            .includes('OBSOLETE_LARGE_PAYLOAD'), 'old payload remains accessible');
        const mainLeaf = manager.getLeafId();
        const main = manager.buildSessionContext();
        assert.deepEqual(main.model, { provider: 'fixture', modelId: 'fixture' });
        assert.equal(main.thinkingLevel, 'low');
        assert.ok(JSON.stringify(main.messages).includes('MAIN_RETAINED_SUMMARY'));
        assert.ok(!JSON.stringify(main.messages).includes('OBSOLETE_LARGE_PAYLOAD'));
        manager.branch('side-compaction');
        const side = JSON.stringify(manager.buildSessionContext().messages);
        assert.ok(side.includes('SIDE_SUMMARY_ONLY') && side.includes('SIDE_BRANCH_ONLY'));
        assert.ok(!side.includes('MAIN_RETAINED_SUMMARY') && !side.includes('ACTIVE_BRANCH_MARKER'));
        manager.branch(mainLeaf);
        const witness = manager.captureCompactionWitness('seed-user');
        assert.throws(() => manager.appendCompactionIfCurrent(witness, proof.old_summary, 2001,
            { agentCommsCommit: proof.old_commit }), /already present|duplicate/i,
        'old commit outside retained context must still prohibit replay');
        assert.equal(fingerprint(), before, 'replay refusal preserves all history');
        // Late malformed data exercises the full incremental scan, with no
        // second large copy and no permission to silently drop the bad record.
        for (const tail of [Buffer.from('{"incomplete":'), Buffer.from('{broken}\n'),
            Buffer.from([0xff, 0x0a]), Buffer.from(JSON.stringify({
                type: 'message', id: 'seed-user', parentId: null,
                message: user('duplicate old ID'), timestamp: '2026-09-28T00:00:00Z',
            }) + '\n')]) {
            try {
                fs.appendFileSync(file, tail);
                const malformedSize = fs.statSync(file).size;
                assert.throws(() => opened(file));
                assert.equal(fs.statSync(file).size, malformedSize, 'failed load never repairs');
            } finally { fs.truncateSync(file, size); }
        }
        assert.equal(fingerprint(), before, 'restored owned fixture retains exact source bytes');
        assert.equal(opened(file).getLeafId(), mainLeaf);
        console.log(JSON.stringify({ large_history_bytes: size, branches: 2,
            old_commit_replay_refused: true, malformed_late_records: 4,
            peak_rss_kib: process.resourceUsage().maxRSS }));
    };
}
const selected = process.argv[2] ? [process.argv[2]] : Object.keys(cases);
try {
    for (const name of selected) {
        assert.ok(cases[name], `unknown case ${name}`);
        cases[name]();
    }
    console.log(JSON.stringify({ ok: true, cases: selected, root }));
} finally {
    for (const manager of managers) manager.entryStore.close();
    fs.rmSync(root, { recursive: true, force: true });
}
