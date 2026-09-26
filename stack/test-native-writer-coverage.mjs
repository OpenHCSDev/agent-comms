// Provider-free adversarial loader/fork/snapshot tests; disposable package only.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';

const packageDir = process.env.PI_NATIVE_PACKAGE_DIR;
assert.ok(packageDir, 'provide disposable PI_NATIVE_PACKAGE_DIR');
const moduleURL = pathToFileURL(join(packageDir, 'dist/core/session-manager.js')).href;
const { SessionManager, loadEntriesFromFile } = await import(moduleURL);
const root = fs.mkdtempSync(join(tmpdir(), 'pr48-writer-coverage-'));
const user = content => ({ role: 'user', content, timestamp: 1 });
const assistant = { role: 'assistant', content: [{ type: 'text', text: 'answer' }],
    provider: 'fixture', model: 'fixture', api: 'fixture', stopReason: 'stop', timestamp: 2 };
function fixture() {
    const manager = SessionManager.create(root, join(root, 'sessions'));
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
function fixedTime(action) {
    const OriginalDate = Date;
    globalThis.Date = class extends OriginalDate {
        constructor(...args) { super(...(args.length ? args : ['2026-01-01T00:00:00.000Z'])); }
    };
    try { return action(); } finally { globalThis.Date = OriginalDate; }
}
const cases = {
    'newline-repair': () => {
        const { file } = fixture();
        const raw = fs.readFileSync(file).subarray(0, -1);
        fs.writeFileSync(file, raw);
        fs.writeFileSync(`${file}.pr48-writer.lock`, 'held\n');
        try {
            assert.throws(() => loadEntriesFromFile(file), /Incomplete/);
            assert.deepEqual(fs.readFileSync(file), raw);
        } finally { fs.unlinkSync(`${file}.pr48-writer.lock`); }
    },
    'malformed-load': () => {
        const { file } = fixture();
        fs.appendFileSync(file, '{broken}\n');
        const raw = fs.readFileSync(file);
        assert.throws(() => SessionManager.open(file));
        assert.deepEqual(fs.readFileSync(file), raw);
    },
    'invalid-utf8-load': () => {
        const { file } = fixture();
        const raw = fs.readFileSync(file);
        raw[raw.indexOf(Buffer.from('task'))] = 0xff;
        fs.writeFileSync(file, raw);
        assert.throws(() => SessionManager.open(file), /encoded data/);
        assert.deepEqual(fs.readFileSync(file), raw);
    },
    'duplicate-header-load': () => {
        const { file } = fixture();
        fs.appendFileSync(file, JSON.stringify({ ...rows(file)[0], id: 'second-header' }) + '\n');
        assert.throws(() => SessionManager.open(file), /ancestry/);
    },
    'orphan-load': () => {
        const { file } = fixture();
        const entries = rows(file);
        entries.at(-1).parentId = 'missing-parent';
        fs.writeFileSync(file, entries.map(JSON.stringify).join('\n') + '\n');
        assert.throws(() => SessionManager.open(file), /ancestry/);
    },
    'legacy-load': () => {
        const { file } = fixture();
        const entries = rows(file);
        entries[0].version = 2;
        fs.writeFileSync(file, entries.map(JSON.stringify).join('\n') + '\n');
        const raw = fs.readFileSync(file);
        assert.throws(() => SessionManager.open(file), /explicit recovery/);
        assert.deepEqual(fs.readFileSync(file), raw);
    },
    'read-snapshot-race': () => {
        const { file } = fixture();
        const original = fs.readFileSync;
        let armed = true;
        fs.readFileSync = (path, ...args) => {
            const result = original(path, ...args);
            if (path === file && armed) {
                armed = false;
                externalAppend(file);
            }
            return result;
        };
        syncBuiltinESMExports();
        try { assert.throws(() => SessionManager.open(file), /changed during load/); }
        finally { fs.readFileSync = original; syncBuiltinESMExports(); }
    },
    'constructor-race': () => {
        const { file } = fixture();
        const before = rows(file).length;
        const load = SessionManager.prototype._loadEntries;
        SessionManager.prototype._loadEntries = function(...args) {
            const result = load.apply(this, args);
            externalAppend(file);
            return result;
        };
        try { assert.throws(() => SessionManager.open(file), /changed during load/); }
        finally { SessionManager.prototype._loadEntries = load; }
        assert.equal(rows(file).length, before + 1);
        assert.equal(rows(file).at(-1).message.content, 'external');
    },
    'set-session-race': () => {
        const { file } = fixture();
        const manager = fixture().manager;
        const load = SessionManager.prototype._loadEntries;
        SessionManager.prototype._loadEntries = function(...args) {
            const result = load.apply(this, args);
            externalAppend(file);
            return result;
        };
        try { assert.throws(() => manager.setSessionFile(file), /changed during load/); }
        finally { SessionManager.prototype._loadEntries = load; }
        const raw = fs.readFileSync(file);
        assert.throws(() => manager.appendMessage(user('must not attach stale sibling')), /writer changed/);
        assert.deepEqual(fs.readFileSync(file), raw);
    },
    'preloaded-stale': () => {
        const { file } = fixture();
        const stale = rows(file);
        externalAppend(file);
        const leaf = rows(file).at(-1).id;
        const manager = new SessionManager(root, dirname(file), file, true, undefined, stale);
        assert.equal(manager.getLeafId(), leaf);
        manager.appendMessage(user('coherent child'));
        assert.equal(rows(file).at(-1).parentId, leaf);
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
        try { assert.throws(() => SessionManager.open(file), /rewrite source changed/); }
        finally { SessionManager.prototype.newSession = fresh; }
        assert.equal(rows(file).length, 3);
        assert.equal(rows(file)[1].message.content, 'external');
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
        assert.throws(() => SessionManager.open(join(target, destination)), /Incomplete/);
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
        assert.equal(SessionManager.open(branch).getLeafId(), manager.getLeafId());
    },
};
const selected = process.argv[2] ? [process.argv[2]] : Object.keys(cases);
for (const name of selected) {
    assert.ok(cases[name], `unknown case ${name}`);
    cases[name]();
}
console.log(JSON.stringify({ ok: true, cases: selected, root }));
