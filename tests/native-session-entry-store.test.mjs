import assert from 'node:assert/strict';
import { test } from 'node:test';
import { appendFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { DiskEntryStore, MemoryEntryStore } from '../stack/native-session-entry-store.mjs';

const scratch = resolve(process.env.TMPDIR ?? '.artifacts');
mkdirSync(scratch, {recursive:true});
const header = {type:'session', version:3, id:'test-session', cwd:scratch};
const entry = (id,parentId,content='text') => ({type:'message',id,parentId,message:{role:'user',content}});
function fixture(rows) {
    const dir = mkdtempSync(join(scratch,'entry-store-'));
    const file = join(dir,'session.jsonl');
    writeFileSync(file,[header,...rows].map(x=>JSON.stringify(x)+'\n').join(''));
    return {dir,file,close:()=>rmSync(dir,{recursive:true,force:true})};
}

test('both stores preserve branch context, settings and archived input',()=>{
    const rows = [
        {type:'model_change',id:'model',parentId:null,provider:'test',modelId:'model'},
        {...entry('a','model'),message:{role:'user',content:'original',inputId:'tracked',inputDigest:'digest'}},
        entry('b','a'), entry('sibling','a'),
        {type:'compaction',id:'compact',parentId:'b',firstKeptEntryId:'b',summary:'summary'},
        entry('c','compact'),
    ];
    const f=fixture(rows);
    const disk = new DiskEntryStore(f.file,{indexDirectory:f.dir});
    const memory=new MemoryEntryStore(header,rows);
    try {
        for (const store of [disk,memory]) {
            assert.deepEqual([...store.contextEntries('c')].map(e=>e.id),['compact','b','c']);
            assert.deepEqual([...store.contextEntries('sibling')].map(e=>e.id),['model','a','sibling']);
            assert.deepEqual(store.contextSettings('c').model,{provider:'test',modelId:'model'});
            assert.equal(store.trackedInput('tracked').id,'a');
            assert.equal(store.trackedInputMetadata('tracked').inputDigest,'digest');
            assert.equal([...store.trackedMetadata()][0].id,'a');
            assert.deepEqual([...store.entries()],rows);
            assert.equal(store.commonAncestor('c','sibling'),'a');
            assert.equal(store.commonAncestor(null,'c'),null);
        }
        const next=entry('d','c');
        appendFileSync(f.file,JSON.stringify(next)+'\n');
        assert.throws(()=>disk.get('a'),/revision changed/);
        disk.committedAppend(next,f.file);
        assert.deepEqual(disk.get('d'),next);
    } finally {disk.close();memory.close();f.close();}
});

test('strict disk scan refuses malformed UTF8, incomplete tail, duplicated IDs and forward parent without modifying history',()=>{
    for (const invalid of [
        Buffer.from(JSON.stringify(header)+'\n'+JSON.stringify(entry('a',null))),
        Buffer.concat([Buffer.from(JSON.stringify(header)+'\n'),Buffer.from([255,10])]),
        Buffer.from([header,entry('a',null),entry('a',null)].map(JSON.stringify).join('\n')+'\n'),
        Buffer.from([header,entry('a','later'),entry('later',null)].map(JSON.stringify).join('\n')+'\n'),
        Buffer.from([header,entry(header.id,null)].map(JSON.stringify).join('\n')+'\n'),
    ]) {
        const f=fixture([]);writeFileSync(f.file,invalid);
        try {assert.throws(()=>new DiskEntryStore(f.file,{indexDirectory:f.dir}));assert.deepEqual(readFileSync(f.file),invalid);}
        finally {f.close();}
    }
});
