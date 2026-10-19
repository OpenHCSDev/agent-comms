import assert from 'node:assert/strict';
import {appendFileSync,mkdtempSync,rmSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {test} from 'node:test';
import {pathToFileURL} from 'node:url';
const [root,scratch]=process.argv.slice(2);
const {DiskEntryStore,MemoryEntryStore}=await import(pathToFileURL(join(root,'dist/core/session-entry-store.js')));
const {SessionManager}=await import(pathToFileURL(join(root,'dist/core/session-manager.js')));
const header={type:'session',version:3,id:'settings-session',cwd:scratch};
const base=(id,parentId,type)=>({id,parentId,type,timestamp:'2026-01-01T00:00:00Z'});
const rows=[{...base('model',null,'model_change'),provider:'provider-a',modelId:'model-a'},
 {...base('thinking','model','thinking_level_change'),thinkingLevel:'high'},
 {...base('compact','thinking','compaction'),summary:'summary',firstKeptEntryId:'thinking',tokensBefore:100},
 {...base('leaf','compact','message'),message:{role:'user',content:'leaf'}},
 {...base('other','model','model_change'),provider:'provider-b',modelId:'model-b'}];
const encode=values=>values.map(row=>JSON.stringify(row)+'\n').join('');
test('append inheritance, branch isolation and close/reopen keep effective settings',()=>{
 const directory=mkdtempSync(join(scratch,'settings-')),file=join(directory,'saved.jsonl');
 writeFileSync(file,encode([header,...rows]));
 const memory=new MemoryEntryStore(header,rows);let disk=new DiskEntryStore(file,{indexDirectory:directory});
 try {
  for(const id of rows.map(row=>row.id))assert.deepEqual(disk.contextSettings(id),memory.contextSettings(id));
  assert.deepEqual(disk.contextSettings('leaf'),{model:{provider:'provider-a',modelId:'model-a'},thinkingLevel:'high'});
  assert.deepEqual(disk.contextSettings('other'),{model:{provider:'provider-b',modelId:'model-b'},thinkingLevel:'off'});
  const next={...base('next','leaf','message'),message:{role:'user',content:'next'}};
  appendFileSync(file,encode([next]));disk.committedAppend(next);memory.append(next);
  assert.deepEqual(disk.contextSettings(),memory.contextSettings());
  disk.close();disk=new DiskEntryStore(file,{indexDirectory:directory});
  assert.deepEqual(disk.contextSettings(),memory.contextSettings());
 }finally{disk.close();memory.close();rmSync(directory,{recursive:true,force:true});}
});
test('SessionManager persists model and thinking changes through the shared append',()=>{
 const directory=mkdtempSync(join(scratch,'settings-sdk-')),file=join(directory,'saved.jsonl');
 writeFileSync(file,encode([header,...rows]));let manager=SessionManager.open(file);
 try {
  manager.appendModelChange('provider-c','model-c');manager.appendThinkingLevelChange('low');
  assert.deepEqual(manager.entryStore.contextSettings(),{model:{provider:'provider-c',modelId:'model-c'},thinkingLevel:'low'});
  manager.entryStore.close();manager=SessionManager.open(file);
  assert.deepEqual(manager.entryStore.contextSettings(),{model:{provider:'provider-c',modelId:'model-c'},thinkingLevel:'low'});
 }finally{manager.entryStore.close();rmSync(directory,{recursive:true,force:true});}
});
