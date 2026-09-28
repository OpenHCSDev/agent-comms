// Disposable, provider-free actual RPC + native compact() selected-stream tests.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
const pkg=process.env.PI_NATIVE_PACKAGE_DIR;
const originalRpc = join(pkg,'dist/modes/rpc/rpc-mode.js');
const integrated = readFileSync(originalRpc,'utf8').includes('case "agent_comms_summarize_compaction"');
assert.ok(pkg && integrated, 'Use the complete owned native candidate');
const root=mkdtempSync(join(tmpdir(),'pr95-selected-summary-'));
const patched=originalRpc;
try {
  const childSource=`
import {runRpcMode} from ${JSON.stringify(patched)};
import {SessionManager, sessionEntryToContextMessages} from ${JSON.stringify(join(pkg,'dist/core/session-manager.js'))};
import {prepareCompaction} from ${JSON.stringify(join(pkg,'dist/core/compaction/index.js'))};
import {SessionContext} from ${JSON.stringify(join(pkg,'dist/core/session-context.js'))};
import {createAssistantMessageEventStream} from ${JSON.stringify(join(pkg,'node_modules/@earendil-works/pi-ai/dist/utils/event-stream.js'))};
import {readFileSync} from 'node:fs';
globalThis.fetch=()=>{throw Error('NETWORK PROHIBITED')};
const manager=SessionManager.create(${JSON.stringify(root)},${JSON.stringify(root)});
for(let i=0;i<5;i++) {
 manager.appendMessage({role:'user',content:'Question '+i,timestamp:2*i});
 manager.appendMessage({role:'assistant',content:[{type:'text',text:'Answer '+i}],provider:'fake',model:'fake',api:'fake',stopReason:'stop',timestamp:2*i+1});
}
const settings={enabled:process.env.PR95_EFFECTIVE_DISABLED !== '1',reserveTokens:1000,keepRecentTokens:10};
const model={provider:'fake',id:'fake',api:'openai-completions',contextWindow:10000,maxTokens:1000};
const preparation=prepareCompaction(manager.entryStore,settings,model);
const witness=manager.captureCompactionWitness(preparation.firstKeptEntryId);
let mode='success',release,callCount=0,blockHooks=false;
if(process.env.PR95_PROVIDER_ERROR === '1') mode='error-stop';
const runner={hasHandlers:()=>blockHooks};
const catalog=process.env.PR95_DECLINE_SUMMARY === '1' ? [] : [model];
const session={sessionManager:manager,sessionFile:manager.getSessionFile(),sessionId:manager.getSessionId(),
  model,modelRuntime:{getAvailableSnapshot:()=>catalog},settingsManager:{getCompactionSettings:()=>settings},
  extensionRunner:runner,_extensionRunnerRef:{current:runner},messages:[],isIdle:true,isStreaming:false,
  isCompacting:false,isRetrying:false,pendingMessageCount:0,_retryAttempt:0,_nativeInterruptIds:null,
  _pendingNextTurnMessages:[],_pendingCustomMessages:[],_pendingBashMessages:[],
  agent:{state:{messages:[]},steeringQueue:{messages:[]},followUpQueue:{messages:[]},subscribe:()=>()=>{},
    streamFunction:(_model,_context,options)=>{
      callCount++;
      process.stderr.write('CALL '+JSON.stringify({maxRetries:options.maxRetries,hasSignal:!!options.signal})+'\\n');
      if(mode==='fail') {
        // Fake SDK fallback would retry three times if maxRetries were omitted.
        for(let attempt=0;attempt<1+(options.maxRetries??3);attempt++)
          process.stderr.write('FAKE_ATTEMPT '+attempt+'\\n');
        throw Error('fake provider failure before network');
      }
      const usage={input:12,output:3,cacheRead:0,cacheWrite:0,totalTokens:15,
        cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
      const events=createAssistantMessageEventStream();
      if(mode==='iterator-throw' || mode==='rejecting-result')
        events[Symbol.asyncIterator]=async function*(){throw Error('fake iterator failure')};
      if(mode==='rejecting-result') events.result=()=>Promise.reject(Error('forged early rejection'));
      if(mode==='rejecting-terminal') {
        events.finalResultPromise=Promise.reject(Error('broken early terminal'));
        void events.finalResultPromise.catch(()=>{});
      }
      void (async()=>{
        if(mode==='hold') await new Promise(resolve=>{release=resolve;options.signal.addEventListener('abort',resolve,{once:true})});
        if(mode==='uncooperative') await new Promise(resolve=>setTimeout(resolve,250));
        if(['malformed','iterator-throw','rejecting-result','rejecting-terminal'].includes(mode)) {
          if(mode==='malformed') events.push({type:1});
          await new Promise(resolve=>setTimeout(resolve,250)); // ignore abort: still running
        }
        const text=mode==='large'?'z'.repeat(270000):'Synthetic summary';
        const final={role:'assistant',content:[{type:'text',text}],
          stopReason:mode.startsWith('error-')?'error':'stop',usage,
          ...(mode.startsWith('error-') ? {errorMessage:mode==='error-long'?'x'.repeat(2000)
            : mode==='error-control'?'402: insufficient\\ncredits\\u001b'
            : '402: insufficient credits on configured model'} : {})};
        const abort=()=>{const error={...final,stopReason:'aborted'};
          events.push({type:'error',reason:'aborted',error});events.end(error)};
        if(options.signal.aborted && !['malformed','iterator-throw','rejecting-result','rejecting-terminal'].includes(mode)) return abort();
        if(mode==='hook-drift') blockHooks=true;
        events.push({type:'start',partial:{...final,content:[]}});
        if(mode==='slow-large') {
          const chunk='q'.repeat(160000);
          events.push({type:'text_delta',contentIndex:0,delta:chunk});
          await new Promise(resolve=>setImmediate(resolve));
          events.push({type:'text_delta',contentIndex:0,delta:chunk});
          await new Promise(resolve=>setImmediate(resolve));
          process.stderr.write('EARLY_ABORT '+options.signal.aborted+'\\n');
          if(options.signal.aborted) return abort();
        }
        events.push({type:'text_delta',contentIndex:0,delta:text,partial:final});
        if(options.signal.aborted && !['malformed','iterator-throw','rejecting-result','rejecting-terminal'].includes(mode)) return abort();
        process.stderr.write('TERMINAL '+mode+'\\n');
        events.push(mode.startsWith('error-')
          ? {type:'error',reason:'error',error:final}
          : {type:'done',reason:'stop',message:final});
        events.end(final);
      })().catch(error=>{const failed={role:'assistant',content:[],stopReason:'error',usage,errorMessage:String(error)};
        events.push({type:'error',reason:'error',error:failed});events.end(failed)});
      return events;
    }},
  async bindExtensions(){},subscribe:()=>()=>{},
  setAutoRetryEnabled(v){mode=v?'hold':'success'},
  setAutoCompactionEnabled(v){mode=v?'fail':'success'},
  setSteeringMode(v){
    if(v==='drift') manager.appendMessage({role:'user',content:'other writer',timestamp:100});
    else mode=v;
  },
  setFollowUpMode(v){blockHooks=v==='hook'},

};
SessionContext.restore(session);
const host={session,setRebindSession(){},async dispose(){}};
process.stderr.write(JSON.stringify({witness,tokensBefore:preparation.tokensBefore,selected:{provider:'fake',modelId:'fake',contextWindow:10000},
 settings:{reserveTokens:1000,keepRecentTokens:10},sessionFile:manager.getSessionFile()})+'\\n');
void runRpcMode(host);
`;
  if (process.env.PR95_RPC_FIXTURE === '1') {
    // Python integration uses this same real RPC with a synthetic stream.
    const fixture = spawn('node', ['--input-type=module', '-e', childSource], {stdio:'inherit'});
    assert.equal(await new Promise(resolve => fixture.on('exit', resolve)), 0);
  } else {
  const child=spawn('node',['--input-type=module','-e',childSource],{stdio:['pipe','pipe','pipe']});
  let stderr='';child.stderr.on('data',data=>{stderr+=data});
  const waiters=new Map(),buffered=new Map();
  createInterface({input:child.stdout}).on('line',line=>{
    let parsed;try{parsed=JSON.parse(line)}catch{return}
    if(parsed.type!=='response')return;
    if(waiters.has(parsed.id)){waiters.get(parsed.id)(parsed);waiters.delete(parsed.id)}else buffered.set(parsed.id,parsed);
  });
  function request(command){
    const result=new Promise(resolve=>buffered.has(command.id)
      ? (resolve(buffered.get(command.id)),buffered.delete(command.id)) : waiters.set(command.id,resolve));
    child.stdin.write(JSON.stringify(command)+'\n');
    return Promise.race([result,new Promise((_,reject)=>setTimeout(()=>reject(Error(`RPC timeout ${command.id}: ${stderr}`)),3000))]);
  }
  const start=Date.now();
  while(!stderr.includes('\n')&&Date.now()-start<3000) await new Promise(resolve=>setTimeout(resolve,10));
  assert.ok(stderr.includes('\n'),stderr);
  const {witness,selected,settings,sessionFile}=JSON.parse(stderr.split('\n')[0]);
  const before=readFileSync(sessionFile);
  const op='a'.repeat(32);
  const base={type:'agent_comms_summarize_compaction',version:1,operationId:op,witness,selected,settings};
  const success=await request({id:'success',...base});
  assert.equal(success.success,true,JSON.stringify(success));
  assert.deepEqual(Object.keys(success.data).sort(),['version','status','operationId','witness','selected','settings','result'].sort());
  assert.equal(success.data.status,'summarized');
  assert.equal(success.data.result.summary.includes('Synthetic summary'),true);
  assert.equal(success.data.result.firstKeptEntryId,witness.firstKeptEntryId);
  assert.deepEqual(success.data.result.details,{readFiles:[],modifiedFiles:[]});
  assert.equal((stderr.match(/CALL /g)||[]).length,1);
  assert.match(stderr,/"maxRetries":0,"hasSignal":true/);
  assert.deepEqual(readFileSync(sessionFile),before,'summary never appends');
  const dup=await request({id:'duplicate',...base});
  assert.deepEqual(dup.data,{version:1,status:'declined',operationId:op,reason:'duplicate_operation'});
  const invalid=await request({id:'invalid',...base,operationId:'INVALID'});
  assert.equal(invalid.success,false);assert.equal('data' in invalid,false);
  const calls=()=>(stderr.match(/CALL /g)||[]).length;
  const beforeHook=calls();
  assert.equal((await request({id:'hook-on',type:'set_follow_up_mode',mode:'hook'})).success,true);
  const hookId='c'.repeat(32);
  assert.deepEqual((await request({id:'hook-refuse',...base,operationId:hookId})).data,
    {version:1,status:'declined',operationId:hookId,reason:'extension_unsupported'});
  assert.equal(calls(),beforeHook);
  await request({id:'hook-off',type:'set_follow_up_mode',mode:'none'});
  await request({id:'error-mode',type:'set_steering_mode',mode:'error-stop'});
  const stopId='e'.repeat(32);
  assert.deepEqual((await request({id:'stop',...base,operationId:stopId})).data,
    {version:1,status:'unknown',operationId:stopId,reason:'402: insufficient credits on configured model'});
  for(const [mode,id,reason] of [
      ['error-long','b'.repeat(32),'x'.repeat(1024)],
      ['error-control','0'.repeat(32),'402: insufficient credits']]) {
    await request({id:mode,type:'set_steering_mode',mode});
    assert.deepEqual((await request({id:mode+'-result',...base,operationId:id})).data,
      {version:1,status:'unknown',operationId:id,reason});
  }
  await request({id:'hold-mode',type:'set_auto_retry',enabled:true});
  const holdId='f'.repeat(32);
  const beforeHold=calls();
  const holding=request({id:'holding',...base,operationId:holdId});
  const waitForCall=()=>new Promise((resolve,reject)=>{
    if(calls()>beforeHold) return resolve();
    const timer=setTimeout(()=>{child.stderr.off('data',seen);reject(Error('held fake stream did not start'))},3000);
    const seen=()=>{if(calls()>beforeHold){clearTimeout(timer);child.stderr.off('data',seen);resolve()}};
    child.stderr.on('data',seen);
  });
  await waitForCall();
  for(const [type,more] of [['set_model',{provider:'fake',modelId:'fake'}],['prompt',{message:'original'}],
      ['compact',{}],['new_session',{}],['set_auto_retry',{enabled:false}]]) {
    const denied=await request({id:`blocked-${type}`,type,...more});
    assert.equal(denied.success,false,type);
    assert.match(denied.error,/summary in flight/);
  }
  assert.deepEqual((await request({id:'second',...base,operationId:'1'.repeat(32)})).data,
    {version:1,status:'declined',operationId:'1'.repeat(32),reason:'in_flight'});
  assert.equal((await request({id:'readiness-during',type:'agent_comms_prepare_compaction',
    version:1,dryRun:true,witness,selected,settings})).data.reason,'busy');
  assert.equal((await request({id:'state-during',type:'get_state'})).data.isCompacting,true);
  const canceled=await request({id:'cancel-hold',type:'agent_comms_cancel_summary',version:1,operationId:holdId});
  assert.deepEqual(canceled.data,{version:1,status:'unknown',operationId:holdId});
  assert.deepEqual((await holding).data,{version:1,status:'unknown',operationId:holdId,
    reason:'Selected summary provider stopped: aborted'});
  assert.equal((await request({id:'state-after',type:'get_state'})).data.isCompacting,false);
  assert.deepEqual(readFileSync(sessionFile),before,'cancel did not write');
  await request({id:'hook-drift-mode',type:'set_steering_mode',mode:'hook-drift'});
  const hookDrift='2'.repeat(32);
  assert.deepEqual((await request({id:'hook-drift',...base,operationId:hookDrift})).data,
    {version:1,status:'unknown',operationId:hookDrift,reason:'Summary completion invalid or state changed'});
  await request({id:'hook-restore',type:'set_follow_up_mode',mode:'none'});
  // The consumer may see malformed events or throw before the producer's
  // terminal. Neither permits mutation while the pinned EventStream.result()
  // remains pending; the delayed fake terminal is the only local join proof.
  for(const [index,kind] of ['malformed','iterator-throw'].entries()) {
    assert.equal((await request({id:`mode-${kind}`,type:'set_steering_mode',mode:kind})).success,true);
    const operationId=(index===0?'6':'7').repeat(32);
    const started=Date.now();
    const outstanding=request({id:`pending-${kind}`,...base,operationId});
    await new Promise(resolve=>setTimeout(resolve,75));
    assert.equal((await request({id:`state-${kind}`,type:'get_state'})).data.isCompacting,true);
    const denied=await request({id:`mutation-${kind}`,type:'set_steering_mode',mode:'success'});
    assert.equal(denied.success,false);
    assert.match(denied.error,/summary in flight/);
    assert.deepEqual((await outstanding).data,{version:1,status:'unknown',operationId,
      reason:kind==='malformed'?'Selected summary stream contained unsupported events'
        : 'fake iterator failure'});
    assert.ok(Date.now()-started>=200,`${kind}: consumer failure released slot before producer terminal`);
    assert.deepEqual(readFileSync(sessionFile),before,`${kind} never wrote session`);
  }
  child.stdin.end();
  assert.equal(await new Promise(resolve=>child.on('exit',resolve)),0,stderr);
  // Native map/synthesis uses the SAME captured stream for every planned chunk.
  // Larger history must complete beyond four calls without replaying any input.
  async function historyCase(repeat, expected, expectedCalls='bounded') {
    const source=childSource.replace("content:'Question '+i",`content:'Question '+i+'x'.repeat(${repeat})`);
    assert.notEqual(source,childSource);
    const other=spawn('node',['--input-type=module','-e',source],{stdio:['pipe','pipe','pipe']});
    let diagnostic='';other.stderr.on('data',data=>{diagnostic+=data});
    const responses=new Map(),waiters=new Map();
    createInterface({input:other.stdout}).on('line',line=>{
      let parsed;try{parsed=JSON.parse(line)}catch{return}
      if(parsed.type!=='response')return;
      if(waiters.has(parsed.id)){waiters.get(parsed.id)(parsed);waiters.delete(parsed.id)}
      else responses.set(parsed.id,parsed);
    });
    const begin=Date.now();
    while(!diagnostic.includes('\n')&&Date.now()-begin<3000) await new Promise(resolve=>setTimeout(resolve,10));
    assert.ok(diagnostic.includes('\n'),diagnostic);
    const f=JSON.parse(diagnostic.split('\n')[0]);
    const snapshot=readFileSync(f.sessionFile);
    const command={id:'long',...base,operationId:'3'.repeat(32),
      witness:f.witness,selected:f.selected,settings:f.settings};
    const result=new Promise(resolve=>responses.has('long')
      ? resolve(responses.get('long')):waiters.set('long',resolve));
    other.stdin.write(JSON.stringify(command)+'\n');
    const reply=await Promise.race([result,
      new Promise((_,reject)=>setTimeout(()=>reject(Error('long RPC timeout '+diagnostic)),3000))]);
    assert.equal(reply.data.status,expected,JSON.stringify(reply)+diagnostic);
    const count=(diagnostic.match(/CALL /g)||[]).length;
    if(expectedCalls==='bounded') assert.ok(count>=2 && count<=4,`native calls ${count}: ${diagnostic}`);
    else if(expectedCalls==='larger') assert.ok(count>4,`larger native plan calls ${count}`);
    else assert.equal(count,expectedCalls);
    assert.deepEqual(readFileSync(f.sessionFile),snapshot,'long summary never appends');
    other.stdin.end();
    assert.equal(await new Promise(resolve=>other.on('exit',resolve)),0,diagnostic);
    return count;
  }
  const longCalls=await historyCase(1700,'summarized');
  const largerCalls=await historyCase(7000,'summarized','larger');
  const sourceCalls=await historyCase(400000,'summarized','larger');
  // A pinned-class instance can override result() to reject early, or corrupt
  // its result promise. Neither rejection is proof of provider completion.
  for(const kind of ['rejecting-result','rejecting-terminal','fail']) {
    const source=childSource.replace("let mode='success'",`let mode='${kind}'`);
    const bad=spawn('node',['--input-type=module','-e',source],{stdio:['pipe','pipe','pipe']});
    let diagnostic='';bad.stderr.on('data',data=>{diagnostic+=data});
    const replies=new Map(),waiters=new Map();
    createInterface({input:bad.stdout}).on('line',line=>{
      let message;try{message=JSON.parse(line)}catch{return}
      if(message.type!=='response')return;
      if(waiters.has(message.id)){waiters.get(message.id)(message);waiters.delete(message.id)}
      else replies.set(message.id,message);
    });
    const beforeReady=Date.now();
    while(!diagnostic.includes('\n')&&Date.now()-beforeReady<3000)
      await new Promise(resolve=>setTimeout(resolve,10));
    assert.ok(diagnostic.includes('\n'),diagnostic);
    const fixture=JSON.parse(diagnostic.split('\n')[0]);
    const snapshot=readFileSync(fixture.sessionFile);
    const send=command=>{
      const answer=new Promise(resolve=>replies.has(command.id)
        ? resolve(replies.get(command.id)):waiters.set(command.id,resolve));
      bad.stdin.write(JSON.stringify(command)+'\n');
      return answer;
    };
    const bounded=promise=>Promise.race([promise,
      new Promise((_,reject)=>setTimeout(()=>reject(Error(`${kind} RPC unresponsive: ${diagnostic}`)),1500))]);
    const operationId=(kind==='rejecting-result'?'8':'9').repeat(32);
    let settled=false;
    void send({id:'unjoined',...base,operationId,witness:fixture.witness,
      selected:fixture.selected,settings:fixture.settings}).then(()=>{settled=true});
    await new Promise(resolve=>setTimeout(resolve,75));
    assert.equal((await bounded(send({id:'early-state',type:'get_state'}))).data.isCompacting,true);
    assert.equal((await bounded(send({id:'early-mutation',type:'set_steering_mode',mode:'success'}))).success,false);
    await new Promise(resolve=>setTimeout(resolve,300));
    if(kind==='fail') {
      assert.match(diagnostic,/FAKE_ATTEMPT 0/);
      assert.equal((diagnostic.match(/FAKE_ATTEMPT /g)||[]).length,1,'no hidden retry');
    } else assert.match(diagnostic,new RegExp(`TERMINAL ${kind}`));
    assert.equal(settled,false,`${kind}: no trusted terminal receipt`);
    assert.equal((await bounded(send({id:'late-state',type:'get_state'}))).data.isCompacting,true);
    assert.equal((await bounded(send({id:'late-mutation',type:'prompt',message:'never'}))).success,false);
    assert.deepEqual(readFileSync(fixture.sessionFile),snapshot);
    bad.kill('SIGKILL'); // disposable exact test child, not a production retirement claim
    await new Promise(resolve=>bad.on('exit',resolve));
  }
  console.log(JSON.stringify({ok:true,calls:calls(),longCalls,largerCalls,sourceCalls}));
  }
} finally {
  if (process.env.PR95_KEEP_SOURCE !== '1') rmSync(root,{force:true,recursive:true});
}
