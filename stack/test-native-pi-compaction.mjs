// Pi's own compaction engine on the prepared package: threshold before a tracked
// input, overflow recovery of a tracked input, and manual /compact with the
// thread's task brief. Local SSE fixture only; no network, credentials or paid calls.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdirSync,mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomBytes} from 'node:crypto';

const mode=process.argv[2] ?? 'threshold';
assert.ok(['threshold','overflow','manual','wip','cap','oversized'].includes(mode));
const pkg=process.env.PI_NATIVE_PACKAGE_DIR;
assert.ok(pkg,'Prepared native package required');
const requests=[];
let overflowSent=false;
let replyCount=0;
// The declared cap reads each response's own usage right after it completes.
const replyUsage=()=> mode!=='cap' ? 1200 : (++replyCount===2 ? 319995 : 1200);
const chunk=(text,promptTokens)=>'data: '+JSON.stringify({id:'local',object:'chat.completion.chunk',created:1,
    model:'pi-compaction',choices:[{index:0,delta:{content:text},finish_reason:'stop'}],
    usage:{prompt_tokens:promptTokens,completion_tokens:5,total_tokens:promptTokens+5}})+'\n\ndata: [DONE]\n\n';
const server=createServer(async(request,response)=>{
    let body='';for await(const part of request)body+=part;
    const payload=JSON.parse(body);
    const text=JSON.stringify(payload.messages);
    const summary=text.includes('<conversation>');
    const inputChars=payload.messages.reduce((total,message)=>total+
        (typeof message.content==='string' ? message.content.length :
            message.content.reduce((chars,block)=>chars+(block.text?.length??0),0)),0);
    requests.push({summary,text,inputChars});
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
    response.end(chunk('TRACKED_REPLY',replyUsage()));
});
const root=mkdtempSync(join(tmpdir(),'native-pi-compaction-'));
let manager,session;
try {
    process.env.AGENT_COMMS_NATIVE_CONFIG_DIR=root;
    process.env.PI_TASK='TASK_BRIEF_FIXTURE';
    await new Promise(done=>server.listen(0,'127.0.0.1',done));
    const fetch=globalThis.fetch;
    globalThis.fetch=(input,options)=>{
        const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);
        assert.equal(url.origin,`http://127.0.0.1:${server.address().port}`,'Only local fixture permitted');
        return fetch(input,options);
    };
    const pi=await import(join(pkg,'dist/index.js'));
    const {writeFileSync}=await import('node:fs');
    writeFileSync(join(root,'models.json'),JSON.stringify({providers:{'compaction-local':{
        baseUrl:`http://127.0.0.1:${server.address().port}/v1`,api:'openai-completions',
        models:[{id:'pi-compaction',name:'Compaction local',
            contextWindow:mode==='cap'?1000000:100000,maxTokens:2048}]}}}));
    const runtime=await pi.ModelRuntime.create({authPath:join(root,'auth.json'),modelsPath:join(root,'models.json'),modelsStorePath:join(root,'models-store.json')});
    await runtime.setRuntimeApiKey('compaction-local','local-only');
    const model=runtime.getModel('compaction-local','pi-compaction');
    const settings=pi.SettingsManager.inMemory({compaction:{enabled:mode!=='cap',reserveTokens:16384,keepRecentTokens:2000},retry:{enabled:false}});
    if(mode==='cap'){
        const budget=settings.getCompactionSettings();
        assert.equal(settings.getCompactionEnabled(),true,'the Core budget cannot be disabled per thread');
        assert.equal(budget.retainedContextTokens,300000,'the payload budget owns the one global threshold');
        assert.equal(pi.shouldCompact(300000,1000000,budget),false,'the exact threshold still fits');
        assert.equal(pi.shouldCompact(300001,1000000,budget),true,'one token over the threshold compacts');
    }
    const loader=new pi.DefaultResourceLoader({cwd:root,agentDir:root,settingsManager:settings,noExtensions:true,noSkills:true,noPromptTemplates:true});
    await loader.reload();
    const sessions=join(root,'sessions');mkdirSync(sessions,{mode:0o700});
    manager=pi.SessionManager.create(root,sessions);
    manager.appendModelChange(model.provider,model.id);
    // Old history (summarized) followed by a recent turn (kept). The last
    // assistant usage puts the context past Pi's threshold (window - reserve).
    const used=(mode==='threshold'||mode==='wip') ? 90000 : (mode==='cap' ? 250000 : 30000);
    const statedLines='Now: adding an event class\nUncommitted: stack/patch test edits\nNext: run the focused tests';
    for(let turn=0;turn<4;turn++){
        const historyRepeats=mode==='oversized' ? 25000 : 1500;
        manager.appendMessage({role:'user',content:`OLD_FACT_${turn} `+'old history '.repeat(historyRepeats),timestamp:2*turn+1});
        const stated=mode==='wip' && turn===1
            ? [{type:'text',text:`Working.\n${statedLines}`}]
            : [{type:'text',text:`old answer ${turn}`}];
        manager.appendMessage({role:'assistant',content:stated,api:model.api,
            provider:model.provider,model:model.id,stopReason:'stop',timestamp:2*turn+2,
            usage:{input:used-100,output:100,cacheRead:0,cacheWrite:0,totalTokens:used,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});
    }
    if(mode==='wip'){
        // The thread's own worktree carries uncommitted changes at compaction.
        const {execFileSync}=await import('node:child_process');
        execFileSync('git',['init','-q'],{cwd:root});
        writeFileSync(join(root,'uncommitted.txt'),'dirty');
    }
    const agentSessionConfig={cwd:root,agentDir:root,modelRuntime:runtime,model,
        sessionManager:manager,settingsManager:settings,resourceLoader:loader,noTools:'all'};
    ({session}=await pi.createAgentSession(agentSessionConfig));
    const events=[];
    session.subscribe(event=>events.push(event));
    const kinds=()=>events.map(event=>event.type);
    const compactions=()=>[...manager.entryStore.branch(manager.getLeafId())].filter(entry=>entry.type==='compaction');
    const inputId=randomBytes(16).toString('hex');
    let result;
    if(mode==='cap'){
        // Under the declared cap with a 1,000,000-token window: no compaction.
        await session.prompt('UNDER_CAP',{inputId:randomBytes(16).toString('hex')});
        assert.equal(compactions().length,0,'context under the declared cap does not compact');
        assert.ok(!requests.at(-1).text.includes('SUMMARY_FIXTURE'),'the request used the full session');
        // The second reply's own usage crosses the retained-context threshold
        // (still far below the model window); Pi compacts after that response.
        await session.prompt('OVER_CAP',{inputId:randomBytes(16).toString('hex')});
        assert.equal(compactions().length,1,'the declared cap compacts right after the crossing response');
        assert.ok(compactions()[0].summary.trimEnd().endsWith('SUMMARY_FIXTURE'),'the generated summary follows whatever the cap carries');
        // The next turn resumes from the compaction summary.
        await session.prompt('RESUME',{inputId:randomBytes(16).toString('hex')});
        assert.ok(requests.at(-1).text.includes('SUMMARY_FIXTURE'),'the next request resumed from the compaction summary');
        assert.ok(!requests.at(-1).text.includes('OLD_FACT_0'),'summarized history left the context');
    } else if(mode==='manual'||mode==='oversized'){
        result=await session.compact('USER_FOCUS');
        if(mode==='manual') assert.equal(requests.length,1);
        assert.ok(requests[0].summary);
        assert.ok(requests[0].text.includes('Additional focus: TASK_BRIEF_FIXTURE\\n\\nUSER_FOCUS'),'brief precedes typed instructions');
        assert.equal(compactions().length,1);
        if(mode==='manual') assert.equal(result.summary.startsWith('SUMMARY_FIXTURE'),true);
        else assert.ok(result.summary.includes('SUMMARY_FIXTURE'));
        await session.prompt('AFTER_MANUAL',{inputId});
    } else {
        await session.prompt('TRACKED_INPUT',{inputId});
    }
    const prompts=requests.filter(request=>!request.summary);
    const summaries=requests.filter(request=>request.summary);
    if(mode!=='cap') assert.equal(compactions().length,1,'Pi wrote exactly one compaction entry');
    if(mode==='oversized'){
        assert.ok(summaries.length>1,'an oversized history is summarized through bounded updates');
        const inputLimit=(model.contextWindow-settings.getCompactionSettings().reserveTokens)*4;
        assert.ok(summaries.every(request=>request.inputChars<=inputLimit),
            'every summarizer payload fits the same model-safe retained-context budget');
        assert.ok(summaries.slice(1).every(request=>request.text.includes('<previous-summary>')),
            'each later chunk updates the preceding summary');
    } else {
        assert.equal(summaries.length,1,'one summary request');
    }
    assert.ok(summaries[0].text.includes('Additional focus: TASK_BRIEF_FIXTURE'),'task brief reaches the summary');
    if(mode==='wip'){
        const carried=compactions()[0].summary;
        assert.ok(carried.startsWith('Work in progress when compaction started:'),'the stated lines ride at the top of the retained context');
        assert.ok(carried.includes('Now: adding an event class'),'the stated Now line is carried verbatim');
        assert.ok(carried.includes('Uncommitted: stack/patch test edits (the worktree has uncommitted changes at compaction)'),'the stated Uncommitted line carries the real worktree fact');
        assert.ok(carried.includes('Next: run the focused tests'),'the stated Next line is carried verbatim');
        assert.ok(carried.includes('SUMMARY_FIXTURE'),'the generated summary follows the carried lines');
    }
    const last=prompts.at(-1).text;
    assert.ok(last.includes('SUMMARY_FIXTURE'),'model request starts from the summary');
    assert.ok(!last.includes('OLD_FACT_0'),'summarized history left the context');
    if(mode==='wip') assert.ok(last.includes('Now: adding an event class'),'the retained context the model sees starts from the carried lines');
    assert.equal(manager.getLeafEntry().message.content[0].text,'TRACKED_REPLY');
    const committed=events.filter(event=>event.type==='context_committed' && event.inputId===inputId);
    const compactionEnds=events.filter(event=>event.type==='compaction_end');
    assert.equal(compactionEnds.length,1);
    if(mode==='manual'||mode==='oversized') assert.equal(compactionEnds[0].reason,'manual');
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
