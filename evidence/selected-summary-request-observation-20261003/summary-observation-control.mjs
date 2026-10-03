/** Changed compiled function/control only; not installed native/ACP acceptance. */
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { SourceTextModule, SyntheticModule } from 'node:vm';

const [projection, donor, receiptPath] = process.argv.slice(2);
const summaryPath = resolve(projection, 'dist/core/compaction/compaction.js');
const donorSummary = resolve(donor, 'dist/core/compaction/compaction.js');
const helperPath = resolve(projection, 'node_modules/@earendil-works/pi-ai/dist/utils/agent-comms-request-observation.js');
const donorURL = pathToFileURL(donorSummary).href;
const resolveImport = name => import.meta.resolve(name, donorURL);
const module = new SourceTextModule(readFileSync(summaryPath, 'utf8'), {identifier: summaryPath});
const imports = [];
await module.link(async name => {
    const selected = name.endsWith('/agent-comms-request-observation.js') ? helperPath
        : name.startsWith('.') ? resolve(dirname(donorSummary), name) : fileURLToPath(resolveImport(name));
    const namespace = await import(pathToFileURL(selected));
    imports.push({name, selected});
    return new SyntheticModule(Object.keys(namespace), function () {
        for (const key of Object.keys(namespace)) this.setExport(key, namespace[key]);
    }, {identifier: selected});
});
await module.evaluate();
const {completeSummarization} = module.namespace;
const {AssistantMessageEventStream} = await import(resolveImport('@earendil-works/pi-ai'));
const {NativeRequestObservation} = await import(pathToFileURL(helperPath));
const usage = {input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,
    cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
const context = {messages:[{role:'user',inputId:'historical-input',content:[{type:'text',text:'source'}],timestamp:1}]};
const model = {id:'control',provider:'local',api:'openai-completions'};
const records = [];
async function run(label, throwing = false, aborted = false) {
    const points = [], text = [], options = {maxTokens:2048, sessionId:label};
    let calls = 0;
    const stream = (actualModel, actualContext, actualOptions) => {
        assert.equal(actualModel, model); assert.equal(actualContext, context);
        assert.equal(actualOptions.maxTokens, options.maxTokens);
        assert.equal(actualOptions.sessionId, label);
        calls++;
        const events = new AssistantMessageEventStream();
        const response = {role:'assistant',content:[{type:'text',text:aborted?'':'summary'}],
            api:'openai-completions',provider:'local',model:'control',usage,
            stopReason:aborted?'aborted':'stop',timestamp:1};
        queueMicrotask(() => {
            events.push({type:'start',partial:response});
            events.push({type:'thinking_delta',contentIndex:0,delta:'thought',partial:response});
            events.push({type:'text_delta',contentIndex:0,delta:'summary',partial:response});
            if (aborted) events.push({type:'error',reason:'aborted',error:response});
            else events.push({type:'done',reason:'stop',message:response});
            events.end(response);
        });
        return events;
    };
    const result = await completeSummarization(model,context,options,stream,
        {enabled:false,maxRetries:0}, {onRequestProgress:p=>{
            points.push(p); if(throwing)throw Error('diagnostic sink unavailable');
        },onSummaryText:t=>text.push(t)});
    assert.equal(calls,1); assert.equal(result.stopReason,aborted?'aborted':'stop');
    assert.deepEqual(text,['summary']);
    assert.deepEqual(points.map(p=>p.stage),['preparing','first_delta_consumed','finished']);
    assert.equal(new Set(points.map(p=>p.requestId)).size,1);
    assert(points.every(p=>p.sessionId===label && p.inputId==='historical-input'));
    assert(points.at(-1).elapsedMs>=points[0].elapsedMs);
    records.push({label,requestId:points[0].requestId, stages:points.map(p=>p.stage),calls,stopReason:result.stopReason});
}
await Promise.all([run('parallel-leaf-one'),run('parallel-leaf-two')]);
assert.notEqual(records[0].requestId, records[1].requestId);
await run('throwing-observer',true);
await run('accepted-abort',false,true);
let closed = false;
async function* original(){try{yield{type:'text_delta'};yield{type:'text_delta'};}finally{closed=true;}}
const points=[];
const observation=new NativeRequestObservation({sessionId:'iterator',onRequestProgress:p=>points.push(p)},context);
for await (const event of observation.events(original())) {assert.equal(event.type,'text_delta');break;}
assert(closed); assert.equal(points.filter(p=>p.stage==='first_delta_consumed').length,1);
writeFileSync(receiptPath,JSON.stringify({records,imports,iterator_return_closes_original:true,
    paid_calls:0,native_children:0,qualified:'Compiled source projection with unchanged donor imports and controlled event streams; no native/ACP installed workflow or provider latency claim'},null,2)+'\n');
console.log(JSON.stringify({controls:records,iterator_return_closes_original:true,paid_calls:0}));
