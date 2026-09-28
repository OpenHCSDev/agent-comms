import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
const pkg = process.env.PI_NATIVE_PACKAGE_DIR;
assert(pkg, 'PI_NATIVE_PACKAGE_DIR must identify the combined candidate');
const { AgentSession } = await import(pathToFileURL(resolve(pkg, 'dist/core/agent-session.js')));
const { SessionManager } = await import(pathToFileURL(resolve(pkg, 'dist/core/session-manager.js')));
const root = mkdtempSync(resolve('.artifacts/tmp/native-proof-manager-'));
const request={text:'old retained input'};
const inputId='1'.repeat(32);
const inputDigest=createHash('sha256').update('pi-input-request-v1\n'+JSON.stringify(request)).digest('hex');
const file=join(root,'session.jsonl'), id=randomUUID();
const entries=[
 {type:'session',version:3,id,cwd:root,timestamp:new Date().toISOString()},
 {type:'message',id:'00000001',parentId:null,message:{role:'user',content:'old retained input',inputId,inputDigest}},
 {type:'message',id:'00000002',parentId:'00000001',message:{role:'assistant',content:'old response'}},
 {type:'message',id:'00000003',parentId:null,message:{role:'user',content:'other branch',inputId:'2'.repeat(32),inputDigest:'c'.repeat(64)}},
 {type:'compaction',id:'00000004',parentId:'00000002',summary:'retained summary',firstKeptEntryId:'00000002',tokensBefore:20},
];
let manager;
try {
 writeFileSync(file,entries.map(e=>JSON.stringify(e)+'\n').join(''),{mode:0o600});
 writeFileSync(file+'.input-proof',JSON.stringify({schema:1,type:'context_committed',sessionId:id,inputId,sessionEntryId:'00000001',requestGeneration:73,llmContextDigest:'b'.repeat(64)})+'\n',{mode:0o600});
 manager=SessionManager.open(file,root);
 const state=Object.assign(Object.create(AgentSession.prototype),{sessionManager:manager,_nativeInputClaims:new Map(),_nativeRequestGeneration:0,_emit(){assert.fail('recovery must not emit');}});
 state._loadNativeInputState();
 assert.equal(state._nativeRequestGeneration,73);
 assert.equal(state._nativeInputClaims.size,0);
 assert.equal(state._claimNativeInput(inputId,request),true);
 assert.throws(()=>state._claimNativeInput(inputId,{text:'changed'}),/Conflicting replay/);
 assert.throws(()=>state._claimNativeInput('2'.repeat(32),request),/Conflicting replay/);
 assert.equal([...manager.entryStore.entries()].length,entries.length-1);
 console.log(JSON.stringify({nativeManager:true,entries:entries.length-1,oldCompactedInput:true,siblingInput:true,generation:73,emissions:0}));
} finally { manager?.entryStore.close(); rmSync(root,{recursive:true,force:true}); }
