#!/usr/bin/env node
// End validation for the shared selected-entry count. No native input/provider.
import assert from 'node:assert/strict';
import {appendFileSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

const pkg = process.env.PI_NATIVE_PACKAGE_DIR;
assert.ok(pkg, 'Prepared native package required');
const root = mkdtempSync(join(tmpdir(), 'native-state-count-'));
const {MemoryEntryStore, DiskEntryStore} = await import(pathToFileURL(join(pkg, 'dist/core/session-entry-store.js')));
const header = {type:'session', version:3, id:'state-count', cwd:root, timestamp:'2026-10-02T00:00:00Z'};
const entries = [
    {type:'message', id:'a', parentId:null, message:{role:'user', content:'first'}},
    {type:'custom_message', id:'b', parentId:'a', customType:'fixture', content:'custom', display:true},
    {type:'branch_summary', id:'c', parentId:'b', summary:''},
    {type:'model_change', id:'d', parentId:'c', provider:'fixture', modelId:'not-dispatched'},
    {type:'branch_summary', id:'e', parentId:'d', summary:'branch'},
    {type:'compaction', id:'f', parentId:'e', summary:'summary', firstKeptEntryId:'b', tokensBefore:100},
    {type:'message', id:'g', parentId:'f', message:{role:'assistant', content:[]}},
    {type:'message', id:'h', parentId:'a', message:{role:'user', content:'other branch'}},
];
const file = join(root, 'session.jsonl');
writeFileSync(file, [header,...entries].map(row => JSON.stringify(row)).join('\n')+'\n', {mode:0o600});
const stores = [new MemoryEntryStore(header, entries), new DiskEntryStore(file, {indexDirectory:root})];
try {
    for (const store of stores) {
        assert.equal(store.contextMessageCount('g'), 4, 'compacted kept suffix excludes earlier source');
        assert.equal(store.contextMessageCount('h'), 2, 'other branch excludes the compaction');
        assert.equal(store.contextMessageCount(null), 0, 'empty branch');
        assert.deepEqual([...store.contextEntries('g')].map(entry => entry.id), ['f','b','c','d','e','g']);
        assert.equal(store.contextMessageCount('g'), 4, 'repeat count preserves selected context');
    }
    appendFileSync(file, '\n');
    assert.throws(() => stores[1].contextMessageCount('g'), /revision changed/, 'stale selectors never answer');
} finally {
    for (const store of stores) store.close();
    rmSync(root, {recursive:true, force:true});
}

console.log(JSON.stringify({ok:true, branches_and_revision_refusal:true}));
