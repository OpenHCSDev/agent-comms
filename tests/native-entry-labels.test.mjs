import assert from 'node:assert/strict';
import {appendFileSync,mkdtempSync,rmSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {test} from 'node:test';
import {pathToFileURL} from 'node:url';
const root=process.argv[2],scratch=process.argv[3];
const {DiskEntryStore,MemoryEntryStore}=await import(pathToFileURL(join(root,'dist/core/session-entry-store.js')));
const {SessionManager}=await import(pathToFileURL(join(root,'dist/core/session-manager.js')));
const header={type:'session',version:3,id:'labels-session',cwd:scratch};
const entry=(id,parentId)=>({type:'message',id,parentId,timestamp:'2026-01-01T00:00:00Z',message:{role:'user',content:id}});
const label=(id,parentId,targetId,text,timestamp='2026-01-01T00:00:00Z')=>({type:'label',id,parentId,targetId,label:text,timestamp});
const rows=[entry('root',null),entry('child','root'),label('old','child','root','old'),label('live','old','child','live','2025-01-01T00:00:00Z'),label('clear','live','root','')];
const encode=rows=>rows.map(row=>JSON.stringify(row)+'\n').join('');
const labels=store=>[...store.labels()].sort((a,b)=>a.targetId.localeCompare(b.targetId));
test('one live label per target, clear records, timestamps, append and disk/memory parity',()=>{
 const directory=mkdtempSync(join(scratch,'labels-')),file=join(directory,'session.jsonl');writeFileSync(file,encode([header,...rows]));
 const disk=new DiskEntryStore(file,{indexDirectory:directory}),memory=new MemoryEntryStore(header,rows);
 try{for(const store of [disk,memory]){assert.deepEqual(labels(store),[{targetId:'child',label:'live',timestamp:'2025-01-01T00:00:00Z'}]);assert.equal(store.label('root'),undefined);assert.deepEqual(store.label('child'),[...store.labels()][0]);}
 const next=label('new','clear','root','new');appendFileSync(file,encode([next]));disk.committedAppend(next);memory.append(next);assert.deepEqual(labels(disk),labels(memory));assert.equal(labels(disk).length,2);
 }finally{disk.close();memory.close();rmSync(directory,{recursive:true,force:true});}
 for(const store of [disk,memory])assert.throws(()=>[...store.labels()],/unusable/);
});
test('getTree keeps payloads, node shape, links and globally resolved labels',()=>{
 const directory=mkdtempSync(join(scratch,'tree-')),file=join(directory,'session.jsonl');writeFileSync(file,encode([header,...rows]));const manager=SessionManager.open(file);
 try{const roots=manager.getTree();assert.equal(roots.length,1);const nodes=new Map(),pending=[...roots];while(pending.length){const node=pending.pop();assert.deepEqual(Object.keys(node),['entry','children','label','labelTimestamp']);nodes.set(node.entry.id,node);pending.push(...node.children);}assert.equal(nodes.size,rows.length);for(const row of rows)assert.deepEqual(nodes.get(row.id).entry,row);assert.equal(nodes.get('root').label,undefined);assert.equal(nodes.get('root').labelTimestamp,undefined);assert.equal(nodes.get('child').label,'live');assert.equal(nodes.get('child').labelTimestamp,'2025-01-01T00:00:00Z');assert.deepEqual(roots[0].children.map(n=>n.entry.id),['child']);
 }finally{manager.entryStore.close();rmSync(directory,{recursive:true,force:true});}
});
test('memory branch copying carries other-branch labels and does not resurrect a cleared label',()=>{
 const manager=SessionManager.inMemory(scratch);
 try{const root=manager.appendMessage({role:'user',content:'root',timestamp:1});const selected=manager.appendLabelChange(root,'old');manager.appendMessage({role:'user',content:'other branch',timestamp:2});manager.appendLabelChange(root,undefined);manager.appendLabelChange(selected,'global');manager.createBranchedSession(selected);assert.equal(manager.getLabel(root),undefined);assert.equal(manager.getLabel(selected),'global');const tree=manager.getTree();assert.equal(tree[0].entry.id,root);assert.equal(tree[0].label,undefined);assert.equal(tree[0].children[0].label,'global');
 }finally{manager.entryStore.close();}
});
