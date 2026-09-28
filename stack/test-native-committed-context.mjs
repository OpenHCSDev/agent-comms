// Real native compaction + localhost SSE + saved-session reopen, no paid calls.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdirSync, mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {join, resolve} from 'node:path';

const pkg=process.env.PI_COMPACTION_TEST_PACKAGE;
assert.ok(pkg, 'Use the complete matched native candidate');
const artifacts=resolve('.artifacts');mkdirSync(artifacts,{recursive:true});
const root=mkdtempSync(join(artifacts,'native-context-'));
process.env.AGENT_COMMS_SESSION_INDEX_DIR=join(root,'indexes');
const {SessionManager, sessionEntryToContextMessages}=await import(join(pkg,'dist/core/session-manager.js'));
const {prepareCompaction, compact}=await import(join(pkg,'dist/core/compaction/compaction.js'));
const {CompactionPolicy}=await import(join(pkg,'dist/core/compaction/agent-comms-policy.js'));
const {SessionContext}=await import(join(pkg,'dist/core/session-context.js'));
const {computeFileLists, formatFileOperations}=await import(join(pkg,'dist/core/compaction/utils.js'));
const requests=[];
const summary='HISTORY_FACT_727 PREFIX_FACT_431 EXACT_PATH_src/domain.py. '+ 'Condensed context. '.repeat(800);
const server=createServer(async(request,response)=>{
    let body='';for await(const chunk of request)body+=chunk;
    const parsed=JSON.parse(body);requests.push(parsed);
    response.writeHead(200, {'content-type':'text/event-stream'});
    const frame=(delta,finish=null,usage)=>'data: '+JSON.stringify({id:'local-summary',object:'chat.completion.chunk',
        created:1,model:'native-local',choices:[{index:0,delta,finish_reason:finish}],...(usage?{usage}:{})})+'\n\n';
    response.write(frame({role:'assistant',content:summary}));
    response.write(frame({},'stop',{prompt_tokens:100,completion_tokens:3200,total_tokens:3300}));
    response.end('data: [DONE]\n\n');
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const model={provider:'fixture',id:'native-local',name:'Local native compaction',api:'openai-completions',
    baseUrl:`http://127.0.0.1:${server.address().port}/v1`,reasoning:false,input:['text'],
    cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:272000,maxTokens:4096};
const settings={enabled:true,reserveTokens:16384,keepRecentTokens:20000};
const policy=CompactionPolicy.fromEnvironment();
const usage={input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,
    cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
function assistant(text,timestamp) {return {role:'assistant',content:[{type:'text',text}],provider:model.provider,
    model:model.id,api:model.api,stopReason:'stop',timestamp,usage};}
try {
    const receipts=[];
    for(const split of [false,true]) {
        const directory=join(root,split?'split':'whole');mkdirSync(directory);
        const manager=SessionManager.create(directory,directory);
        manager.appendMessage({role:'user',content:'HISTORY_FACT_727 '+ 'older history '.repeat(5000),timestamp:1});
        manager.appendMessage(assistant('Historical answer',2));
        manager.appendMessage({role:'user',content:'PREFIX_FACT_431 EXACT_PATH_src/domain.py '+ 'current prefix '.repeat(5000),timestamp:3});
        manager.appendMessage(assistant('retained answer '.repeat(1500),4));
        if(!split)manager.appendMessage({role:'user',content:'latest request',timestamp:5});
        const paths=Array.from({length:500},(_,i)=>`src/${i}-`+'nested-directory/'.repeat(5)+'file.py');
        // A real earlier compaction owns the full file ledger; preparation must
        // include it even if provider-generated prose omits its projection.
        const retained=manager.getLeafId();
        manager.appendCompaction('HISTORY_FACT_727',retained,100000,{readFiles:paths,modifiedFiles:[]});
        manager.appendMessage({role:'user',content:'PREFIX_FACT_431 '+ 'new prefix '.repeat(5000),timestamp:6});
        manager.appendMessage(assistant('kept tail '.repeat(2300),7));
        if(!split)manager.appendMessage({role:'user',content:'latest request',timestamp:8});
        const actualSettings={...settings,keepRecentTokens:1};
        const original=readFileSync(manager.getSessionFile());
        const preparation=prepareCompaction(manager.entryStore,actualSettings,model);
        assert.ok(preparation);
        assert.equal(preparation.isSplitTurn,split);
        const files=computeFileLists(preparation.fileOps);
        const annotations=formatFileOperations(files.readFiles,files.modifiedFiles);
        const before=requests.length;
        const result=await compact(preparation,model,'local-test',{},'Preserve every source part',
            undefined,'off',undefined,{},{enabled:false,maxRetries:0},undefined,undefined);
        assert.equal(readFileSync(manager.getSessionFile()).equals(original),true,'generation must not append');
        const prompts=requests.slice(before).map(request=>JSON.stringify(request.messages));
        assert.ok(prompts.some(prompt=>prompt.includes('HISTORY_FACT_727')),'history source must reach the provider');
        assert.ok(prompts.some(prompt=>prompt.includes('PREFIX_FACT_431')),'entire prefix source must reach the provider');
        if(split)assert.ok(result.summary.includes('**Turn Context (split turn):**'));
        assert.ok(result.summary.includes('HISTORY_FACT_727'));
        assert.ok(result.summary.includes('PREFIX_FACT_431'));
        assert.ok(result.summary.endsWith(annotations));
        assert.deepEqual(result.details,{readFiles:paths.sort(),modifiedFiles:[]});
        manager.appendCompaction(result.summary,result.firstKeptEntryId,result.tokensBefore,result.details,false,result.usage);
        const saved=manager.getSessionFile();manager.entryStore.close();
        const reopened=SessionManager.open(saved);
        const messages=reopened.buildContextEntries().flatMap(sessionEntryToContextMessages);
        const bytes=messages.reduce((total,message)=>total+policy.messageBytes(message),0);
        assert.ok(bytes<=policy.inputBytes(model,actualSettings.reserveTokens));
        const session={sessionManager:reopened,model,settingsManager:{getCompactionSettings:()=>actualSettings},agent:{state:{messages:[]}}};
        SessionContext.restore(session);
        assert.equal(session.storedContext.requiresCompaction(),false,'actual restored context must be admitted');
        const last=reopened.entryStore.latest(reopened.getLeafId(),'compaction');
        assert.deepEqual(last.details.readFiles,paths.sort());
        reopened.entryStore.close();
        receipts.push({split,providerRequests:requests.length-before,generatedBytes:Buffer.byteLength(summary),
            annotationBytes:Buffer.byteLength(annotations),committedBytes:bytes,inputBytes:policy.inputBytes(model,actualSettings.reserveTokens),readyAfterReopen:true});
    }
    console.log(JSON.stringify({paidRequests:0,cases:receipts},null,2));
} finally {
    await new Promise(resolve=>server.close(resolve));
    rmSync(root,{recursive:true,force:true});
}
