// Small source-native journey, reusing the installed SDK without changing it.
// The resolve hook substitutes only our reviewed EntryStore module; this is
// source acceptance, not pinned-package or installed application acceptance.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {registerHooks, syncBuiltinESMExports} from 'node:module';
import {join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';

const [root, packageRoot, worktree, operation] = process.argv.slice(2);
assert(root && packageRoot && worktree);
const source = pathToFileURL(join(worktree, 'stack/native-session-entry-store.mjs')).href;
registerHooks({resolve(specifier, context, next) {
    if (specifier === './session-manager.js' && context.parentURL === source)
        return next(pathToFileURL(join(packageRoot, 'dist/core/session-manager.js')).href, context);
    const resolved = next(specifier, context);
    if (resolved.url === pathToFileURL(join(packageRoot, 'dist/core/session-entry-store.js')).href)
        return {...resolved, url:source};
    return resolved;
}});
if (operation === 'helper') {
    const original = fs.statSync(join(root, 'session.jsonl'), {bigint:true});
    const read = fs.readSync;
    fs.readSync = (fd, ...args) => {
        const observed = fs.fstatSync(fd, {bigint:true});
        assert(!(original.dev === observed.dev && original.ino === observed.ino),
            'A warm helper in a fresh process must not read original JSONL bytes');
        return read(fd, ...args);
    };
    syncBuiltinESMExports();
    await import(pathToFileURL(join(worktree, 'src/agent_comms/_pi_helpers/native_index.mjs')));
} else if (operation === 'store-tests') {
    assert(!fs.existsSync(root), 'Refuse to reuse a test root');
    fs.mkdirSync(root, {mode:0o700});
    process.env.TMPDIR = root;
    await import(pathToFileURL(join(worktree, 'tests/native-session-entry-store.test.mjs')));
} else {
    await journey();
}

async function journey() {
assert(!fs.existsSync(root), 'Refuse to reuse an operation root');
fs.mkdirSync(root, {mode:0o700});
let bytesRead = 0;
const originalRead = fs.readSync;
fs.readSync = (...args) => {const n = originalRead(...args); bytesRead += n; return n;};
syncBuiltinESMExports();
const {DiskEntryStore, MemoryEntryStore, ReadOnlyEntryStore} = await import(source);
const {SessionManager} = await import(pathToFileURL(join(packageRoot, 'dist/core/session-manager.js')));
const {DatabaseSync} = await import('node:sqlite');
const file = join(root, 'session.jsonl');
const timestamp = '1970-01-01T00:00:00.000Z';
const header = {type:'session', version:3, id:'01a13289-0cc0-7155-a4ed-6142e4d26868', timestamp, cwd:root};
const entries = [];
for (let i=0; i<1000; i++) entries.push({
    type:'message', id:`entry${i}`, parentId:i ? `entry${i-1}` : null, timestamp,
    message:{role:'user', content:[{type:'text', text:`original ${i}`}], timestamp:0},
});
entries[3].message = {role:'assistant', content:[
    {type:'toolCall', id:'first', name:'read', arguments:{path:'first'}},
    {type:'toolCall', id:'last', name:'bash', arguments:{command:'printf original', large:JSON.rawJSON('9007199254740993')}},
], api:'openai-responses', provider:'openai', model:'test', stopReason:'toolUse', timestamp:0};
// An opaque extension's similarly named payload is not a native message.
entries[999] = {...entries[999], type:'custom', customType:'extension', data:{}, message:{
    role:'assistant', inputId:'not-a-native-input', content:[
        {type:'toolCall', id:'not-a-native-call', name:'bash', arguments:{}},
    ],
}};
fs.writeFileSync(file, [header, ...entries].map(x=>JSON.stringify(x)).join('\n')+'\n', {mode:0o600});
const original = createHash('sha256').update(fs.readFileSync(file)).digest('hex');
let store = new DiskEntryStore(file);
const coldBytes = bytesRead;
assert.equal(fs.statSync(DiskEntryStore.indexFile(file)).mode & 0o777, 0o600);
assert.equal(store.latestToolMetadata().id, 'entry3');
assert.equal(store.latestToolMetadata().toolCallCount, 2);
assert.equal(store.metadata('entry999').role, null);
assert.equal(store.metadata('entry999').inputId, null);
assert.equal(store.metadata('entry999').model, null);
store.close();
bytesRead = 0;
store = new DiskEntryStore(file);
assert.equal(store.latestToolMetadata().id, 'entry3');
assert.equal(bytesRead, 0, 'Warm open and selection must not read JSONL');
store.close();
const manager = SessionManager.open(file, root);
assert.equal(manager.entryStore.latestToolMetadata().id, 'entry3');
assert.equal(bytesRead, 0, 'Real native saved-session startup must reuse the index');
assert.equal(createHash('sha256').update(fs.readFileSync(file)).digest('hex'), original);
const appended = manager.appendMessage({role:'user', content:[{type:'text', text:'distinct test append'}], timestamp:1});
manager.entryStore.close();
bytesRead = 0;
store = new DiskEntryStore(file);
assert.equal(store.lastId, appended);
assert.equal(store.latestToolMetadata().id, 'entry3');
assert.equal(bytesRead, 0, 'Native writer must commit a reusable selector cut');
store.close();
const warmJsonlBytes = bytesRead;
const unindexed = join(root, 'unindexed.jsonl');
fs.writeFileSync(unindexed, fs.readFileSync(file));
bytesRead = 0;
assert.throws(()=>new ReadOnlyEntryStore(unindexed));
assert.equal(bytesRead, 0, 'A query must not build a missing index by scanning history');
assert(!fs.existsSync(DiskEntryStore.indexFile(unindexed)), 'A query must not create an index');
const helper = spawnSync(process.execPath, [import.meta.filename, root, packageRoot, worktree, 'helper'], {
    input:JSON.stringify({package:packageRoot, file}), encoding:'utf8', env:process.env,
});
assert.equal(helper.status, 0, helper.stderr);
const observation = JSON.parse(helper.stdout);
assert.equal(observation.latest_tool.entry_id, 'entry3');
fs.writeFileSync(join(root, 'observation.json'), helper.stdout);
const db = new DatabaseSync(DiskEntryStore.indexFile(file));
const plan = db.prepare("EXPLAIN QUERY PLAN SELECT selectors FROM entries WHERE json_extract(selectors,'$.toolCallCount')>0 ORDER BY sequence DESC LIMIT 1").all();
assert(plan.some(row=>row.detail.includes('_tool')), 'Latest call must use the partial index');
assert(!plan.some(row=>row.detail.includes('TEMP B-TREE')));
db.exec('BEGIN IMMEDIATE');
try {
    const duringWriter = spawnSync(process.execPath,
        [import.meta.filename, root, packageRoot, worktree, 'helper'], {
            input:JSON.stringify({package:packageRoot, file}), encoding:'utf8', env:process.env,
        });
    assert.equal(duringWriter.status, 0, duringWriter.stderr);
    assert.equal(JSON.parse(duringWriter.stdout).latest_tool.entry_id, 'entry3');
} finally { db.exec('ROLLBACK'); }
assert.equal(db.prepare("SELECT count(*) AS n FROM sqlite_master WHERE name LIKE 'building_%' AND type='table'").get().n, 0);
db.close();
const memory = new MemoryEntryStore(header, entries);
assert.equal(memory.latestToolMetadata().id, 'entry3');
memory.close();
const empty = join(root, 'no-tools.jsonl');
fs.writeFileSync(empty, [header, {...entries[0], parentId:null}].map(x=>JSON.stringify(x)).join('\n')+'\n');
const emptyStore = new DiskEntryStore(empty);
assert.equal(emptyStore.latestToolMetadata(), undefined);
emptyStore.close();
// Partial bytes are unavailable, never an apparently empty complete source.
const partial = join(root, 'partial.jsonl');
fs.writeFileSync(partial, fs.readFileSync(file));
fs.appendFileSync(partial, '{"type":');
assert.throws(()=>new DiskEntryStore(partial), /Incomplete native session/);
bytesRead = 0;
assert.throws(()=>new ReadOnlyEntryStore(partial), /Exact native index unavailable/);
assert.equal(bytesRead, 0, 'An unavailable/stale index must not initiate preparation');
// A dead preparer is collectible; a live preparer's table is never stolen.
const partialIndex = DiskEntryStore.indexFile(partial);
const abandoned = `building_${helper.pid}_` + 'a'.repeat(32);
const active = `building_${process.pid}_` + 'b'.repeat(32);
let partialDb = new DatabaseSync(partialIndex);
partialDb.exec(`CREATE TABLE ${abandoned}(value); CREATE TABLE ${active}(value)`);
partialDb.close();
fs.writeFileSync(partial, fs.readFileSync(file));
const recovered = new DiskEntryStore(partial);
recovered.close();
partialDb = new DatabaseSync(partialIndex);
assert.equal(partialDb.prepare('SELECT name FROM sqlite_schema WHERE name=?').get(abandoned), undefined);
assert.equal(partialDb.prepare('SELECT name FROM sqlite_schema WHERE name=?').get(active).name, active);
partialDb.exec(`DROP TABLE ${active}`);
partialDb.close();
fs.writeFileSync(join(root, 'result.json'), JSON.stringify({
    accepted:true, entries:entries.length, coldBytes, warmJsonlBytes,
    realHelperAnswered:true, freshHelperReadOriginalBytes:false, originalNoToolProved:true,
    coldQueryReadOriginalBytes:false, coldQueryCreatedIndex:false, queryDuringWriterAnswered:true,
    originalUnchangedBeforeDistinctAppend:true, nativeWriterReopen:true,
    deadPreparationRetired:true, activePreparationPreserved:true, plan,
    installed:false, packageRoot, source:resolve(worktree),
}, null, 2)+'\n');
console.log(fs.readFileSync(join(root, 'result.json'), 'utf8'));
}
