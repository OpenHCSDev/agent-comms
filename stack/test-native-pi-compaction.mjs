// Pi's own compaction engine on the prepared package: threshold before a tracked
// input, overflow recovery of a tracked input, and manual /compact with the
// thread's task brief. Local SSE fixture only; no network, credentials or paid calls.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdirSync,mkdtempSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {randomBytes} from 'node:crypto';

const mode=process.argv[2] ?? 'threshold';
assert.ok(['threshold','overflow','manual'].includes(mode));
const pkg=process.env.PI_COMPACTION_TEST_PACKAGE;
assert.ok(pkg,'Prepared native package required');
const artifacts=resolve('.artifacts');mkdirSync(artifacts,{recursive:true,mode:0o700});
const root=mkdtempSync(join(artifacts,'pi-compaction-'));
process.env.AGENT_COMMS_SESSION_INDEX_DIR=join(root,'indexes');
process.env.AGENT_COMMS_NATIVE_CONFIG_DIR=root;
process.env.PI_TASK='TASK_BRIEF_FIXTURE';
const requests=[];
let overflowSent=false;
const chunk=(text,promptTokens)=>'data: '+JSON.stringify({id:'local',object:'chat.completion.chunk',created:1,
    model:'pi-compaction',choices:[{index:0,delta:{content:text},finish_reason:'stop'}],
    usage:{prompt_tokens:promptTokens,completion_tokens:5,total_tokens:promptTokens+5}})+'\n\ndata: [DONE]\n\n';
const server=createServer(async(request,response)=>{
    let body='';for await(const part of request)body+=part;
    const payload=JSON.parse(body);
    const text=JSON.stringify(payload.messages);
    const summary=text.includes('<conversation>');
    requests.push({summary,text});
    if(summary){
        response.writeHead(200,{'content-type':'text/event-stream'});
        return response.end(chunk('SUMMARY_FIXTURE',500));
    }
    if(mode==='overflow' && !overflowSent){
        overflowSent=true;
        response.writeHead(400,{'content-type':'application/json'});
        return response.end(JSON.stringify({error:{message:"This model's maximum context length is 100000 tokens",type:'invalid_request_error'}}));
    }
    response.writeHead(200,{'content-type':'text/event-stream'});
    response.end(chunk('TRACKED_REPLY',1200));
});
await new Promise(done=>server.listen(0,'127.0.0.1',done));
const fetch=globalThis.fetch;
globalThis.fetch=(input,options)=>{
    const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);
    assert.equal(url.origin,`http://127.0.0.1:${server.address().port}`,'Only local fixture permitted');
    return fetch(input,options);
};
const pi=await import(join(pkg,'dist/index.js'));
let manager,session;
try {
    const {writeFileSync}=await import('node:fs');
    writeFileSync(join(root,'models.json'),JSON.stringify({providers:{'compaction-local':{
        baseUrl:`http://127.0.0.1:${server.address().port}/v1`,api:'openai-completions',
        models:[{id:'pi-compaction',name:'Compaction local',contextWindow:100000,maxTokens:2048}]}}}));
    const runtime=await pi.ModelRuntime.create({authPath:join(root,'auth.json'),modelsPath:join(root,'models.json'),modelsStorePath:join(root,'models-store.json')});
    await runtime.setRuntimeApiKey('compaction-local','local-only');
    const model=runtime.getModel('compaction-local','pi-compaction');
    const settings=pi.SettingsManager.inMemory({compaction:{enabled:true,reserveTokens:16384,keepRecentTokens:2000},retry:{enabled:false}});
    const loader=new pi.DefaultResourceLoader({cwd:root,agentDir:root,settingsManager:settings,noExtensions:true,noSkills:true,noPromptTemplates:true});
    await loader.reload();
    const sessions=join(root,'sessions');mkdirSync(sessions,{mode:0o700});
    manager=pi.SessionManager.create(root,sessions);
    manager.appendModelChange(model.provider,model.id);
    // Old history (summarized) followed by a recent turn (kept). The last
    // assistant usage puts the context past Pi's threshold (window - reserve).
    const used=mode==='threshold' ? 90000 : 30000;
    for(let turn=0;turn<4;turn++){
        manager.appendMessage({role:'user',content:`OLD_FACT_${turn} `+'old history '.repeat(1500),timestamp:2*turn+1});
        manager.appendMessage({role:'assistant',content:[{type:'text',text:`old answer ${turn}`}],api:model.api,
            provider:model.provider,model:model.id,stopReason:'stop',timestamp:2*turn+2,
            usage:{input:used-100,output:100,cacheRead:0,cacheWrite:0,totalTokens:used,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});
    }
    ({session}=await pi.createAgentSession({cwd:root,agentDir:root,modelRuntime:runtime,model,
        sessionManager:manager,settingsManager:settings,resourceLoader:loader,noTools:'all'}));
    const events=[];
    session.subscribe(event=>events.push(event));
    const kinds=()=>events.map(event=>event.type);
    const compactions=()=>[...manager.entryStore.branch(manager.getLeafId())].filter(entry=>entry.type==='compaction');
    const inputId=randomBytes(16).toString('hex');
    let result;
    if(mode==='manual'){
        result=await session.compact('USER_FOCUS');
        assert.equal(requests.length,1);
        assert.ok(requests[0].summary);
        assert.ok(requests[0].text.includes('Additional focus: TASK_BRIEF_FIXTURE\\n\\nUSER_FOCUS'),'brief precedes typed instructions');
        assert.equal(compactions().length,1);
        assert.equal(result.summary.startsWith('SUMMARY_FIXTURE'),true);
        await session.prompt('AFTER_MANUAL',{inputId});
    } else {
        await session.prompt('TRACKED_INPUT',{inputId});
    }
    const prompts=requests.filter(request=>!request.summary);
    const summaries=requests.filter(request=>request.summary);
    assert.equal(compactions().length,1,'Pi wrote exactly one compaction entry');
    assert.equal(summaries.length,1,'one summary request');
    assert.ok(summaries[0].text.includes('Additional focus: TASK_BRIEF_FIXTURE'),'task brief reaches the summary');
    const last=prompts.at(-1).text;
    assert.ok(last.includes('SUMMARY_FIXTURE'),'model request starts from the summary');
    assert.ok(!last.includes('OLD_FACT_0'),'summarized history left the context');
    assert.equal(manager.getLeafEntry().message.content[0].text,'TRACKED_REPLY');
    const committed=events.filter(event=>event.type==='context_committed' && event.inputId===inputId);
    const compactionEnds=events.filter(event=>event.type==='compaction_end');
    assert.equal(compactionEnds.length,1);
    if(mode==='manual') assert.equal(compactionEnds[0].reason,'manual');
    if(mode==='threshold'){
        assert.equal(prompts.length,1,'compacted before the tracked input was sent; nothing resent');
        assert.equal(compactionEnds[0].reason,'threshold');
        assert.equal(committed.length,1);
    }
    if(mode==='overflow'){
        assert.equal(prompts.length,2,'one refused request, one retry after compaction');
        assert.equal(compactionEnds[0].reason,'overflow');
        assert.equal(compactionEnds[0].willRetry,true);
        assert.equal(committed.length,2,'each assembled request commits the tracked input context');
        assert.equal(kinds().filter(kind=>kind==='input_committed').length,1,'the input is persisted once');
    }
    console.log(JSON.stringify({mode,providerRequests:requests.length,summaries:summaries.length,
        compactionEntries:compactions().length,contextCommitted:committed.length,
        events:kinds().filter(kind=>!['message_update'].includes(kind))},null,1));
} finally {
    session?.dispose();manager?.entryStore.close();
    await new Promise(done=>server.close(done));
    rmSync(root,{recursive:true,force:true});
}
