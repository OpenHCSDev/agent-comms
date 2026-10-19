import assert from 'node:assert/strict';
import {appendFileSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {test} from 'node:test';
import {pathToFileURL} from 'node:url';

const {DiskEntryStore, MemoryEntryStore} = await import(pathToFileURL(join(process.argv[2], 'dist/core/session-entry-store.js')));
const scratch = process.argv[3];
const header = {type:'session',version:3,id:'children-session'};
const entry = (id,parentId) => ({type:'message',id,parentId,message:{role:'user',content:id}});
const rows = [entry('old',null),entry('parent',null),entry('first','parent'),
    {type:'compaction',id:'cut',parentId:'first',firstKeptEntryId:'parent',summary:'summary'},
    entry('other',null),entry('second','parent')];
const encode = values => values.map(value => JSON.stringify(value)+'\n').join('');
const ids = values => [...values].map(value => value.id);

test('indexed children preserve order, roots, missing parents and append parity', () => {
    const directory = mkdtempSync(join(scratch,'children-'));
    const file = join(directory,'session.jsonl');
    writeFileSync(file,encode([header,...rows]));
    const disk = new DiskEntryStore(file,{indexDirectory:directory});
    const memory = new MemoryEntryStore(header,rows);
    try {
        for (const parent of ['parent','first','second',null,'missing'])
            assert.deepEqual(ids(disk.children(parent)),ids(memory.children(parent)));
        assert.deepEqual(ids(disk.children('parent')),['first','second']);
        const appended = entry('third','parent');
        appendFileSync(file,encode([appended]));
        disk.committedAppend(appended);
        memory.append(appended);
        assert.deepEqual(ids(disk.children('parent')),ids(memory.children('parent')));
        assert.deepEqual(ids(disk.children('parent')),['first','second','third']);
    } finally {disk.close();memory.close();rmSync(directory,{recursive:true,force:true});}
    assert.throws(() => ids(disk.children('parent')),/unusable/);
});

test('a parent bounds lazy indexing without omitting older siblings', () => {
    const directory = mkdtempSync(join(scratch,'children-cut-'));
    const file = join(directory,'session.jsonl');
    // The malformed older branch must remain unread for this parent, while
    // both children on opposite sides of a compaction must be returned.
    writeFileSync(file,encode([header])+'x'.repeat(200000)+'\n'+encode(rows.slice(1)));
    const disk = new DiskEntryStore(file,{indexDirectory:directory});
    try {
        assert.deepEqual(ids(disk.children('parent')),['first','second']);
        assert.deepEqual(ids(disk.children('parent')),['first','second']);
        // All roots require proving completeness, not silently dropping history.
        assert.throws(() => ids(disk.children(null)));
        assert.throws(() => ids(disk.children('parent')),/unusable/);
    } finally {disk.close();rmSync(directory,{recursive:true,force:true});}
});
