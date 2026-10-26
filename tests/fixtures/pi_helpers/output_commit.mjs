/** Real SDK event/persistence ordering; no provider and no live user state. */
import assert from 'node:assert/strict';
import {mkdirSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const [pkg, root] = process.argv.slice(2);
globalThis.fetch = async () => {throw new Error('Output contract forbids network');};
const pi = await import(pathToFileURL(join(pkg, 'dist/index.js')));
const cwd=join(root,'project'), agentDir=join(root,'config');
mkdirSync(cwd,{recursive:true,mode:0o700});mkdirSync(agentDir,{recursive:true,mode:0o700});
const modelRuntime=await pi.ModelRuntime.create({authPath:join(agentDir,'auth.json'),
    modelsPath:join(agentDir,'models.json'),modelsStorePath:join(agentDir,'models-store.json')});
const settingsManager=pi.SettingsManager.inMemory({retry:{enabled:false}});
const resourceLoader=new pi.DefaultResourceLoader({cwd,agentDir,settingsManager});
await resourceLoader.reload();
const sessionManager=pi.SessionManager.create(cwd,join(root,'sessions'));
sessionManager.appendMessage({role:'user',content:'Original question',timestamp:1});
const {session}=await pi.createAgentSession({cwd,agentDir,modelRuntime,settingsManager,
    resourceLoader,sessionManager});
const usage={input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,
    cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
const assistant={role:'assistant',content:[{type:'text',text:'Saved π answer'},
    {type:'toolCall',id:'parallel-a',name:'read',arguments:{}},
    {type:'toolCall',id:'parallel-b',name:'read',arguments:{}}],
    api:'openai-completions',provider:'fixture',model:'fixture',stopReason:'toolUse',timestamp:2,usage};
const result={role:'toolResult',toolCallId:'parallel-b',toolName:'read',isError:false,
    content:[{type:'text',text:'Only B finished'}],timestamp:3};
const observed=[];
session.subscribe(event=>{
    if (event.type==='turn_end') {
        assert.equal(event.roundCommitted.sessionEntryId,sessionManager.getLeafId());
        const row=JSON.parse(readFileSync(event.roundCommitted.sessionFile).subarray(
            event.roundCommitted.sessionEntryStart,event.roundCommitted.sessionEntryEnd));
        assert.equal(row.message.toolCallId,'parallel-b');
        observed.push(event);
    }
    if (event.type==='message_end') observed.push({type:event.type,end:sessionManager.entryStore.lastId});
    if (event.type==='native_output_committed') {
        const previous=observed.at(-1);
        assert.equal(previous.type,'message_end');
        assert.notEqual(previous.end,event.sessionEntryId,'message_end must precede append');
        const bytes=readFileSync(event.sessionFile);
        assert.equal(bytes[event.sessionEntryEnd-1],10);
        const entry=JSON.parse(bytes.subarray(event.sessionEntryStart,event.sessionEntryEnd));
        assert.equal(entry.id,event.sessionEntryId);
        assert.equal(event.sessionId,session.sessionId);
        const meta=sessionManager.entryStore.metadata(entry.id);
        assert.equal(event.sessionEntryStart,meta.offset);
        assert.equal(event.sessionEntryEnd,meta.offset+meta.length+1);
        observed.push(event);
    }
});
try {
    await session._handleAgentEvent({type:'message_end',message:assistant});
    const firstResult={...result,toolCallId:'parallel-a'};
    await session._handleAgentEvent({type:'message_end',message:firstResult});
    assert.equal(observed.filter(event=>event.type==='turn_end').length,0);
    await session._handleAgentEvent({type:'message_end',message:result});
    const markers=observed.filter(event=>event.type==='native_output_committed');
    assert.deepEqual(markers.map(event=>event.output),[
        {kind:'assistant'}, {kind:'tool',toolCallId:'parallel-a'}, {kind:'tool',toolCallId:'parallel-b'},
    ]);
    assert.ok(markers[0].sessionEntryEnd < markers[1].sessionEntryEnd);
    assert.equal(observed.filter(event=>event.type==='turn_end').length,0);
    await session._handleAgentEvent({type:'turn_end',message:assistant,toolResults:[firstResult,result]});
    assert.equal(observed.at(-1).type,'turn_end');
    assert.equal(observed.at(-1).roundCommitted.sessionEntryEnd,markers.at(-1).sessionEntryEnd);
    console.log(JSON.stringify([...markers,observed.at(-1)]));
} finally {session.dispose();sessionManager.entryStore.close();}
