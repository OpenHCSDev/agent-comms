// Real compiled compaction algorithm, controlled summary transport; no credentials.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const manifest = readFileSync(resolve(import.meta.dirname, 'pi-native.sha256'));
const build = createHash('sha256').update(manifest).digest('hex').slice(0, 16);
const path = resolve(import.meta.dirname, `.pi-native-${build}/node_modules/@earendil-works/pi-coding-agent/dist/core/compaction/compaction.js`);
const { generateSummaryWithUsage } = await import(pathToFileURL(path).href);
const usage = { input: 3, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 5,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
const body = Array.from({length: 40}, (_, i) => `SOURCE_${String(i).padStart(2, '0')} ${'x'.repeat(12000)}\n`).join('');
const model = { provider: 'openrouter', id: 'fake', contextWindow: 128000, maxTokens: 8192 };
async function run(stream, callbacks = {}, signal) {
  return generateSummaryWithUsage([{role:'user',content:[{type:'text',text:body}],timestamp:1}],
    model, 16384, 'local-only', {}, signal, 'PRESERVE_CUSTOM_INSTRUCTION',
    'PREVIOUS_SUMMARY_SENTINEL', undefined, stream, {},
    {enabled:false,maxRetries:0,provider:{maxRetries:0}}, callbacks, undefined);
}
let active=0, peak=0, requestCount=0;
const mapped=[], completed=[], progress=[];
const stream=async (_model, context) => ({result: async()=>{
  const prompt=context.messages[0].content[0].text;
  const index=requestCount++;
  assert.ok(Buffer.byteLength(prompt,'utf8') <= (128000-16384)*.75);
  assert.ok(prompt.includes('PRESERVE_CUSTOM_INSTRUCTION'));
  if(prompt.includes('Combine these chronological segment summaries')){
    assert.equal(active,0,'synthesis waits for all map responses');
    for(let i=0;i<mapped.length;i++) assert.ok(prompt.includes(`MAP_${i}`));
    assert.deepEqual([...prompt.matchAll(/MAP_(\d+)/g)].map(m=>Number(m[1])),mapped.map((_,i)=>i),'synthesis preserves chronological source order');
    return {stopReason:'stop',content:[{type:'text',text:'FINAL_SUMMARY'}],usage};
  }
  mapped.push(prompt); active++;peak=Math.max(peak,active);
  await new Promise(resolve=>setTimeout(resolve,(index%4===0?40:5)));
  active--;completed.push(index);
  return {stopReason:'stop',content:[{type:'text',text:`MAP_${index}`}],usage};
}});
const result=await run(stream,{onSummaryResponse:(_usage,item)=>progress.push(item)});
assert.equal(peak,4,'large history must use four bounded concurrent maps');
assert.notDeepEqual(completed,[...completed].sort((a,b)=>a-b),'fixture completes out of source order');
assert.equal(result.text,'FINAL_SUMMARY');
assert.equal(result.usage.totalTokens,requestCount*usage.totalTokens,'each provider response counted once');
assert.ok(mapped.some(p=>p.includes('PREVIOUS_SUMMARY_SENTINEL')));
for(let i=0;i<40;i++) assert.equal(mapped.filter(p=>p.includes(`SOURCE_${String(i).padStart(2,'0')}`)).length,1);
assert.equal(progress.length,requestCount);
assert.ok(progress.every((p,i)=>p.sourceBytesDone >= (progress[i-1]?.sourceBytesDone ?? 0)));
assert.equal(progress.at(-1).summaryPhase,'synthesis');
assert.equal(progress.at(-1).sourceBytesDone,progress.at(-1).sourceBytesTotal);
let failedCalls=0, aborted=0;
const failing=async(_model,_context,options)=>({result:async()=>{
  const index=failedCalls++;
  if(index===0){await new Promise(r=>setTimeout(r,10));throw new Error('UNCERTAIN_MAP');}
  await new Promise((resolve,reject)=>{
    if(options.signal.aborted){aborted++;reject(new Error('aborted'));return;}
    options.signal.addEventListener('abort',()=>{aborted++;reject(new Error('aborted'));},{once:true});
  });
}});
await assert.rejects(()=>run(failing),/UNCERTAIN_MAP/);
assert.equal(failedCalls,4,'failure must not schedule more maps or synthesis');
assert.equal(aborted,3,'all remaining in-flight summaries must be aborted');
console.log(`parallel compaction PASS maps=${mapped.length} requests=${requestCount} concurrency=${peak}`);
const { CompactionPolicy } = await import(pathToFileURL(resolve(path, '../agent-comms-policy.js')).href);
assert.equal(new CompactionPolicy().workers,4);
assert.equal(new CompactionPolicy({strategy:'serial',concurrency:4}).workers,1);
assert.equal(new CompactionPolicy({strategy:'parallel',concurrency:2}).workers,2);
assert.throws(()=>new CompactionPolicy({strategy:'provider-native'}),/Invalid/);
assert.throws(()=>new CompactionPolicy({concurrency:0}),/Invalid/);
assert.throws(()=>new CompactionPolicy({concurrency:1.5}),/Invalid/);
assert.throws(()=>new CompactionPolicy({retry:true}),/Unknown/);
assert.throws(()=>new CompactionPolicy({inputBudgetRatio:1}),/Invalid/);
assert.throws(()=>new CompactionPolicy().inputBytes({contextWindow:0},16384),/unavailable/);
let serialActive=0,serialPeak=0;
process.env.AGENT_COMMS_COMPACTION_POLICY=JSON.stringify({strategy:'serial'});
await run(async()=>({result:async()=>{
  serialActive++;serialPeak=Math.max(serialPeak,serialActive);
  await new Promise(resolve=>setTimeout(resolve,1));serialActive--;
  return {stopReason:'stop',content:[{type:'text',text:'serial summary'}],usage};
}}));
assert.equal(serialPeak,1);
delete process.env.AGENT_COMMS_COMPACTION_POLICY;
const abortedBeforeStart = new AbortController();abortedBeforeStart.abort(new Error('OWNER_STOP'));
let afterStopCalls=0;
await assert.rejects(()=>run(async()=>{afterStopCalls++;throw new Error('must not run');},{},abortedBeforeStart.signal),/OWNER_STOP/);
assert.equal(afterStopCalls,0);
let hierarchyCalls=0,synthesisCalls=0;
await run(async(_model,context)=>({result:async()=>{
  hierarchyCalls++;
  const prompt=context.messages[0].content[0].text;
  const synth=prompt.includes('Combine these chronological segment summaries');
  if(synth)synthesisCalls++;
  return {stopReason:'stop',content:[{type:'text',text:synth?'bounded intermediate summary':'preserve reference '.repeat(2000)}],usage};
}}));
assert.ok(synthesisCalls>1,'large map outputs require bounded hierarchy, never truncation');
console.log(`policy/hierarchy PASS serialPeak=${serialPeak} synthesisRequests=${synthesisCalls} total=${hierarchyCalls}`);
// Three successive compactions prove adapter plumbing preserves the supplied
// prior summary, newest corrections, and exact evidence across map boundaries.
// This controlled summarizer is not evidence of an LLM's semantic retention.
let prior='FACT_GOAL_A FACT_PATH_src/owner.py FACT_UNRESOLVED_TIMEOUT FACT_DECISION_OLD';
for(let round=1;round<=3;round++){
  const source=`FACT_ROUND_${round}_START\n${'history '.repeat(60000)}\nFACT_DECISION_NEW_${round} FACT_CALLID_abc123`;
  const saved=await generateSummaryWithUsage([{role:'user',content:[{type:'text',text:source}],timestamp:round}],
    model,16384,'local-only',{},undefined,undefined,prior,undefined,
    async(_model,context)=>({result:async()=>{
      const prompt=context.messages[0].content[0].text;
      const facts=[...new Set(prompt.match(/FACT_[A-Za-z0-9_/.-]+/g) ?? [])];
      return {stopReason:'stop',content:[{type:'text',text:facts.join(' ') || 'No new marked facts.'}],usage};
    }}),{},{enabled:false,maxRetries:0},{},undefined);
  for(const fact of ['FACT_GOAL_A','FACT_PATH_src/owner.py','FACT_UNRESOLVED_TIMEOUT','FACT_CALLID_abc123',`FACT_DECISION_NEW_${round}`])
    assert.ok(saved.text.includes(fact),`round ${round} dropped ${fact}`);
  assert.ok(saved.text.indexOf('FACT_DECISION_OLD') < saved.text.indexOf(`FACT_DECISION_NEW_${round}`));
  prior=saved.text;
}
console.log('three-round source/summary plumbing PASS');
