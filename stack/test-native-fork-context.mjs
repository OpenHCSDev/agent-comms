// Real restored/forked native context -> first local-provider reply. No paid calls.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdirSync,mkdtempSync,readFileSync,rmSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';

const mode=process.argv[2] ?? 'fork';
const percent=Number(process.argv[3] ?? 24);
assert.ok(['fork','restore'].includes(mode));
const pkg=process.env.PI_COMPACTION_TEST_PACKAGE;
assert.ok(pkg,'Matched installed native bundle required');
const artifacts=resolve('.artifacts');mkdirSync(artifacts,{recursive:true});
const root=mkdtempSync(join(artifacts,'fork-context-'));
process.env.AGENT_COMMS_SESSION_INDEX_DIR=join(root,'indexes');
process.env.AGENT_COMMS_NATIVE_CONFIG_DIR=root;
const requests=[];
const server=createServer(async(request,response)=>{
    let body='';for await(const chunk of request)body+=chunk;
    const payload=JSON.parse(body);requests.push(payload);
    response.writeHead(200,{'content-type':'text/event-stream'});
    response.end('data: '+JSON.stringify({id:'local-fork',object:'chat.completion.chunk',created:1,
        model:'fork-context',choices:[{index:0,delta:{content:'FIRST_CHILD_REPLY'},finish_reason:'stop'}],
        usage:{prompt_tokens:24500,completion_tokens:5,total_tokens:24505}})+'\n\ndata: [DONE]\n\n');
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const fetch=globalThis.fetch;
globalThis.fetch=(input,options)=>{
    const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);
    assert.equal(url.origin,`http://127.0.0.1:${server.address().port}`,'Only local fixture permitted');
    return fetch(input,options);
};
const pi=await import(join(pkg,'dist/index.js'));
let parent,child,session,runtime;
try {
    writeFileSync(join(root,'models.json'),JSON.stringify({providers:{'fork-local':{
        baseUrl:`http://127.0.0.1:${server.address().port}/v1`,api:'openai-completions',
        models:[{id:'fork-context',name:'Fork local',contextWindow:100000,maxTokens:2048}]}}}));
    runtime=await pi.ModelRuntime.create({authPath:join(root,'auth.json'),modelsPath:join(root,'models.json'),modelsStorePath:join(root,'models-store.json')});
    await runtime.setRuntimeApiKey('fork-local','local-only');
    const model=runtime.getModel('fork-local','fork-context');
    const settings=pi.SettingsManager.inMemory({compaction:{enabled:true,reserveTokens:2048,keepRecentTokens:1000},retry:{enabled:false}});
    const loader=new pi.DefaultResourceLoader({cwd:root,agentDir:root,settingsManager:settings,noExtensions:true,noSkills:true,noPromptTemplates:true});
    await loader.reload();
    parent=pi.SessionManager.create(root,join(root,'parent'));
    parent.appendModelChange(model.provider,model.id);
    parent.appendMessage({role:'user',content:'PARENT_BRANCH_FACT '+ 'repeatable history '.repeat(18000),timestamp:1});
    parent.appendMessage({role:'assistant',content:[{type:'text',text:'parent response'}],api:model.api,
        provider:model.provider,model:model.id,stopReason:'stop',timestamp:2,
        usage:{input:1000,output:1000,cacheRead:percent*1000-2000,cacheWrite:0,totalTokens:percent*1000,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});
    const leaf=parent.getLeafId();
    if(mode==='fork') {
        child=pi.SessionManager.forkFrom(parent.getSessionFile(),root,join(root,'child'));
        parent.appendMessage({role:'user',content:'AFTER_FORK_MUST_NOT_APPEAR',timestamp:3});
    } else {
        const file=parent.getSessionFile();parent.entryStore.close();parent=undefined;
        child=pi.SessionManager.open(file);
    }
    const captured=readFileSync(child.getSessionFile());
    assert.equal(child.getLeafId(),leaf);
    assert.equal(readFileSync(child.getSessionFile()).equals(captured),true);
    ({session}=await pi.createAgentSession({cwd:root,agentDir:root,modelRuntime:runtime,model,
        sessionManager:child,settingsManager:settings,resourceLoader:loader,noTools:'all'}));
    const before=session.getContextUsage();
    assert.equal(before.tokens,percent*1000);assert.equal(before.percent,percent);
    assert.equal(child.entryStore.latest(child.getLeafId(),'compaction'),undefined);
    await session.prompt('FIRST_CHILD_INPUT');
    assert.equal(requests.length,1,'No summary or replay call');
    const requestText=JSON.stringify(requests[0].messages);
    assert.ok(requestText.includes('PARENT_BRANCH_FACT'));
    assert.ok(requestText.includes('FIRST_CHILD_INPUT'));
    assert.ok(!requestText.includes('AFTER_FORK_MUST_NOT_APPEAR'));
    assert.equal(child.entryStore.latest(child.getLeafId(),'compaction'),undefined);
    assert.equal(child.getLeafEntry().message.content[0].text,'FIRST_CHILD_REPLY');
    console.log(JSON.stringify({mode,nativeParentLeaf:leaf,parentPercent:before.percent,contextWindow:before.contextWindow,
        nativeTokens:before.tokens,capturedBytes:captured.length,providerRequests:requests.length,
        compactionEntries:0,firstReply:'FIRST_CHILD_REPLY',parentAfterForkExcluded:true},null,2));
} finally {
    session?.dispose();child?.entryStore.close();parent?.entryStore.close();
    await new Promise(resolve=>server.close(resolve));
    rmSync(root,{recursive:true,force:true});
}
