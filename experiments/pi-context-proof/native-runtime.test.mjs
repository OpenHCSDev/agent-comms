import assert from 'node:assert/strict';
import fs, { chmodSync, readFileSync, existsSync, mkdirSync, mkdtempSync, renameSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';

const packageDir = process.env.PI_PACKAGE_DIR;
if (!packageDir || !packageDir.startsWith('/var/tmp/')) {
  throw new Error('PI_PACKAGE_DIR must name a disposable copied Pi under /var/tmp');
}
const pi = await import(pathToFileURL(join(packageDir, 'dist/index.js')).href);
const ID_A = 'a'.repeat(32);
const ID_B = 'b'.repeat(32);
const ID_C = 'c'.repeat(32);
const ID_D = 'd'.repeat(32);
const emptyUsage = {
  input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};
function fakeResponse(model) {
  const message = {
    role: 'assistant', content: [{type:'text', text:'READY'}],
    api: model.api, provider: model.provider, model: model.id,
    usage: emptyUsage, stopReason: 'stop', timestamp: Date.now(),
  };
  return {
    async *[Symbol.asyncIterator]() { yield { type: 'done', message }; },
    async result() { return message; },
  };
}
async function withSession(callback, loaderOptions = {}, settingsOverrides = {}) {
  const root = mkdtempSync(join(tmpdir(), 'pi-native-identity-'));
  const cwd = join(root, 'work');
  const agentDir = join(root, 'agent');
  const sessions = join(root, 'sessions');
  try {
    const runtime = await pi.ModelRuntime.create({
      authPath: join(root, 'auth.json'), modelsPath: join(root, 'models.json'),
      modelsStorePath: join(root, 'models-store.json'),
    });
    await runtime.setRuntimeApiKey('openai', 'fake-local-only');
    const model = runtime.getModel('openai', 'gpt-4.1-mini');
    assert(model);
    const settings = pi.SettingsManager.inMemory({
      compaction: { enabled: false }, retry: { enabled: false }, ...settingsOverrides,
    });
    const resolvedOptions = typeof loaderOptions === 'function'
      ? loaderOptions({root,cwd}) : loaderOptions;
    const loader = new pi.DefaultResourceLoader({
      cwd, agentDir, settingsManager: settings, ...resolvedOptions,
    });
    await loader.reload();
    const sm = pi.SessionManager.create(cwd, sessions);
    const {session} = await pi.createAgentSession({
      cwd, agentDir, modelRuntime: runtime, model, sessionManager: sm,
      settingsManager: settings, resourceLoader: loader, noTools: 'all',
    });
    try { await callback({ root, session, sm, model, runtime, settings, loader, cwd, agentDir }); }
    finally { session.dispose(); }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}
function records(file) {
  assert(existsSync(file), `missing durable file ${file}`);
  return readFileSync(file, 'utf8').trim().split('\n').map(JSON.parse);
}

test('real SDK pipeline durably binds two identical prompts to distinct input IDs before fake provider', async () => {
  await withSession(async ({session, sm, model}) => {
    const events = [];
    const requests = [];
    session.subscribe(event => {
      if (event.type === 'context_committed' || event.type === 'input_committed') events.push(event);
    });
    session.agent.streamFunction = async (_model, context) => {
      requests.push(context.messages.filter(m => m.role === 'user').map(m => m.inputId));
      const disk = records(sm.getSessionFile());
      assert(disk.some(e => e.type === 'message' && e.message.inputId === requests.at(-1).at(-1)));
      assert(records(sm.getSessionFile() + '.input-proof').length >= requests.length);
      return fakeResponse(model);
    };
    await session.prompt('identical text', { inputId: ID_A });
    await session.prompt('identical text', { inputId: ID_B });
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[0], [ID_A]);
    assert.deepEqual(requests[1], [ID_A, ID_B]);
    assert.deepEqual(events.filter(e => e.type === 'input_committed').map(e=>e.inputId), [ID_A, ID_B]);
    const committed = events.filter(e=>e.type === 'context_committed');
    assert(committed.some(e=>e.inputId === ID_A && e.requestGeneration === 1));
    assert(committed.some(e=>e.inputId === ID_B && e.requestGeneration === 2));
    const before = sm.getEntries().length;
    await session.prompt('identical text', { inputId: ID_B });
    assert.equal(sm.getEntries().length, before, 'exact replay must not duplicate a user or assistant');
    assert.equal(requests.length, 2);
    await assert.rejects(session.prompt('different text', { inputId: ID_B }), /Conflicting replay/);
    assert.equal(requests.length, 2);
  });
});

test('direct core prompt cannot forge a native receipt without a prior claim', async () => {
  await withSession(async ({session,sm,model}) => {
    const inputId='f'.repeat(32);
    let providerStarts=0;
    const receipts=[];
    session.agent.streamFunction=async()=>{providerStarts++;return fakeResponse(model);};
    session.subscribe(event=>{
      if(event.type==='input_committed' || event.type==='context_committed') receipts.push(event);
    });
    assert.equal(session._nativeInputClaims.has(inputId),false);
    await session.agent.prompt([{role:'user',content:[{type:'text',text:'forged direct core input'}],
      timestamp:Date.now(),inputId}]);
    assert.equal(providerStarts,0,'an unclaimed direct core prompt cannot reach provider');
    assert.deepEqual(receipts,[],'no native receipt without an accepted claim');
    assert.equal(session._nativeInputClaims.has(inputId),false);
    assert.equal(sm.getTrackedInput(inputId),undefined,'no unclaimed persisted user entry');
    assert.equal(existsSync(sm.getSessionFile()+'.input-proof'),false);
  });
});

test('direct core prompt with mismatched claimed digest fails before append', async () => {
  await withSession(async ({session,sm,model}) => {
    let providerStarts=0,receipts=0;
    session.agent.streamFunction=async()=>{providerStarts++;return fakeResponse(model);};
    session.subscribe(event=>{
      if(event.type==='input_committed'||event.type==='context_committed') receipts++;
    });
    session._claimNativeInput(ID_A,{message:'trusted text'});
    assert.match(session._nativeInputClaims.get(ID_A),/^[a-f0-9]{64}$/);
    await session.agent.prompt([{role:'user',content:[{type:'text',text:'forged text'}],
      timestamp:Date.now(),inputId:ID_A,inputDigest:'0'.repeat(64)}]);
    assert.equal(providerStarts,0);
    assert.equal(receipts,0);
    assert.equal(sm.getTrackedInput(ID_A),undefined);
    assert.equal(existsSync(sm.getSessionFile()+'.input-proof'),false);
    session._releaseUnqueuedNativeInput(ID_A);
    assert.equal(session._nativeInputClaims.has(ID_A),false);
  });
});

test('context hook rejects an unclaimed user even when a matching legacy row exists', async () => {
  await withSession(async ({session,sm,model}) => {
    const inputId='f'.repeat(32);
    const user={role:'user',content:[{type:'text',text:'legacy row'}],
      timestamp:Date.now(),inputId};
    sm.appendMessage(user);
    let providerStarts=0,receipts=0;
    session.agent.streamFunction=async()=>{providerStarts++;return fakeResponse(model);};
    session.subscribe(event=>{if(event.type==='context_committed') receipts++;});
    assert(sm.getTrackedInput(inputId),'the local row exists without a native claim');
    await assert.rejects(session._commitNativeContext({messages:[user]}),/unbound native input ID/);
    assert.equal(providerStarts,0);
    assert.equal(receipts,0);
    assert.equal(existsSync(sm.getSessionFile()+'.input-proof'),false);
  });
});

test('native ID survives an actual input extension and prompt template expansion', async () => {
  const extensionFactories=[(api)=> {
    api.on('input', event => ({action:'transform',text:event.text.replace('MAGIC','/native')}));
  }];
  const loaderOptions=({cwd}) => {
    mkdirSync(cwd,{recursive:true});
    const filePath=join(cwd,'native.md');
    writeFileSync(filePath,'Expanded actual prompt template');
    return {extensionFactories,promptsOverride:()=>({
      prompts:[{name:'native',description:'Native template',source:'custom',
        filePath,content:'Expanded actual prompt template'}],diagnostics:[],
    })};
  };
  await withSession(async ({session,model}) => {
    let sent=0;
    session.agent.streamFunction=async (_m,context) => {
      sent++;
      const user=context.messages.at(-1);
      assert.equal(user.inputId,ID_A);
      assert.match(user.content[0].text,/Expanded actual prompt template/);
      return fakeResponse(model);
    };
    await session.prompt('MAGIC',{inputId:ID_A,source:'rpc'});
    assert.equal(sent,1);
  }, loaderOptions);
});

test('context transform preserving ID succeeds; removing tracked ID fails closed before provider', async () => {
  await withSession(async ({session, model}) => {
    let sent = 0;
    const original = session.agent.transformContext;
    session.agent.transformContext = async (messages, signal) => {
      const result = original ? await original(messages, signal) : messages;
      return result.map(m => m.role === 'user'
        ? {...m, content: [{type:'text',text:'transformed!'}]} : m);
    };
    session.agent.streamFunction = async (_m, context) => {
      ++sent;
      assert.equal(context.messages.at(-1).inputId, ID_A);
      assert.equal(context.messages.at(-1).content[0].text, 'transformed!');
      return fakeResponse(model);
    };
    await session.prompt('original', {inputId: ID_A});
    assert.equal(sent, 1);
    session.agent.transformContext = async (messages) => messages.map(m=>m.role==='user'
      ? {...m, inputId: undefined} : m);
    await session.prompt('second', {inputId: ID_B});
    assert.equal(sent, 1, 'provider must not receive context with stripped tracked identity');
    assert(!records(session.sessionFile + '.input-proof').some(row=>row.inputId===ID_B));
  });
});

test('queued follow-up clear is not committed; same ID can be submitted again', async () => {
  await withSession(async ({session, model}) => {
    let release;
    const hold = new Promise(resolve=> {release=resolve;});
    let entered;
    const active = new Promise(resolve => {entered=resolve;});
    let count = 0;
    session.agent.streamFunction = async () => {
      ++count;
      if (count === 1) { entered(); await hold; }
      return fakeResponse(model);
    };
    const first = session.prompt('first', {inputId: ID_A});
    await active;
    await session.prompt('identical queued', {inputId: ID_B, streamingBehavior:'followUp'});
    assert.deepEqual(session.clearQueue().followUp, ['identical queued']);
    release();
    await first;
    assert(!session.sessionManager.getTrackedInput(ID_B));
    await session.prompt('identical queued', {inputId: ID_B});
    assert.equal(count, 2);
  });
});

for (const {label, kind, streamingBehavior} of [
  {label:'direct steer', kind:'steer'},
  {label:'direct followUp', kind:'followUp'},
  {label:'streaming steer prompt', kind:'steer', streamingBehavior:'steer'},
  {label:'streaming followUp prompt', kind:'followUp', streamingBehavior:'followUp'},
]) {
  for (const disposition of ['deliver', 'clear']) {
    test(`${label}: throwing queue_update cannot create an inert native ID (${disposition})`, async () => {
      await withSession(async ({session,sm,model}) => {
        let release, entered;
        const held=new Promise(resolve=>{release=resolve;});
        const active=new Promise(resolve=>{entered=resolve;});
        let providerStarts=0;
        session.agent.streamFunction=async()=>{
          if(++providerStarts===1) {entered();await held;}
          return fakeResponse(model);
        };
        const first=session.prompt('first',{inputId:ID_A});
        await active;
        const queue=kind==='steer' ? session.agent.steeringQueue : session.agent.followUpQueue;
        const local=()=>kind==='steer' ? session.getSteeringMessages() : session.getFollowUpMessages();
        const enqueue=async id=>streamingBehavior
          ? session.prompt('identical queued',{inputId:id,streamingBehavior})
          : kind==='steer'
            ? session.steer('identical queued',undefined,id)
            : session.followUp('identical queued',undefined,id);
        const unsubscribe=session.subscribe(event=>{
          const field=kind==='steer' ? 'steering' : 'followUp';
          if(event.type==='queue_update' && event[field].includes('identical queued')) {
            throw Error('real SDK queue observer failed');
          }
        });
        try {
          try {
            await assert.rejects(enqueue(ID_B),/real SDK queue observer failed/);
          } finally {unsubscribe();}
          assert.deepEqual(local(),['identical queued']);
          assert.deepEqual(queue.messages.map(item=>item.inputId),[ID_B]);
          assert.equal(session._nativeInputClaims.has(ID_B),true);
          assert.equal(sm.getTrackedInput(ID_B),undefined);
          await enqueue(ID_B);
          assert.deepEqual(queue.messages.map(item=>item.inputId),[ID_B],
            'exact replay cannot enqueue a second core message');
          if(disposition==='clear') {
            assert.deepEqual(session.clearQueue()[kind==='steer'?'steering':'followUp'],
              ['identical queued']);
            assert.deepEqual(queue.messages,[]);
            assert.equal(session._nativeInputClaims.has(ID_B),false);
            release();await first;
            assert.equal(sm.getTrackedInput(ID_B),undefined);
            await session.prompt('identical queued',{inputId:ID_B});
            assert.equal(providerStarts,2,'cleared ID can be submitted as a fresh prompt');
          } else {
            await enqueue(ID_C);
            assert.deepEqual(queue.messages.map(item=>item.inputId),[ID_B,ID_C],
              'identical text must preserve independent core IDs');
            release();await first;
            assert.equal(providerStarts,3);
            const users=records(sm.getSessionFile())
              .filter(row=>row.type==='message'&&row.message.role==='user');
            assert.deepEqual(users.map(row=>row.message.inputId),[ID_A,ID_B,ID_C]);
            const proofs=records(sm.getSessionFile()+'.input-proof');
            assert(proofs.some(row=>row.inputId===ID_B));
            assert(proofs.some(row=>row.inputId===ID_C));
          }
        } finally {
          unsubscribe();release();await first;
        }
      });
    });
  }
}

for (const {label, kind, streamingBehavior} of [
  {label:'direct steer', kind:'steer'},
  {label:'direct followUp', kind:'followUp'},
  {label:'streaming steer prompt', kind:'steer', streamingBehavior:'steer'},
  {label:'streaming followUp prompt', kind:'followUp', streamingBehavior:'followUp'},
]) {
  test(`${label}: throwing dequeue queue_update cannot strand a tracked input`, async () => {
    await withSession(async ({session,sm,model}) => {
      let entered, release;
      const active=new Promise(resolve=>{entered=resolve;});
      const held=new Promise(resolve=>{release=resolve;});
      let providerStarts=0;
      session.agent.streamFunction=async (_model,context)=>{
        if(++providerStarts===1){entered();await held;}
        if(providerStarts===2){
          assert.equal(context.messages.at(-1).inputId,ID_B);
          assert(records(sm.getSessionFile()+'.input-proof').some(row=>row.inputId===ID_B),
            'native proof must precede model transport');
        }
        return fakeResponse(model);
      };
      const first=session.prompt('first',{inputId:ID_A});
      await active;
      const enqueue=()=>streamingBehavior
        ? session.prompt('second',{inputId:ID_B,streamingBehavior})
        : session[kind]('second',undefined,ID_B);
      await enqueue();
      const core=kind==='steer' ? session.agent.steeringQueue : session.agent.followUpQueue;
      assert.deepEqual(core.messages.map(item=>item.inputId),[ID_B]);
      let threw=0, observed=0;
      const unsubscribe=session.subscribe(event=>{
        const field=kind==='steer' ? 'steering' : 'followUp';
        if(event.type==='queue_update' && event[field].length===0 && threw++===0){
          throw Error('observer threw after core dequeue');
        }
      });
      const witness=session.subscribe(event=>{
        if(event.type==='message_end' && event.message.inputId===ID_B) observed++;
      });
      try {
        release();await first;
        assert.equal(threw,1);
        assert.equal(observed,1,'a throwing subscriber must not hide later observers');
        assert.equal(providerStarts,2,'the selected queued input reaches fake provider once');
        assert.deepEqual(core.messages,[]);
        assert.equal(session._nativeInputClaims.has(ID_B),true);
        assert(sm.getTrackedInput(ID_B));
        const users=records(sm.getSessionFile()).filter(row=>
          row.type==='message' && row.message.inputId===ID_B);
        assert.equal(users.length,1);
        assert(records(sm.getSessionFile()+'.input-proof').some(row=>row.inputId===ID_B));
        await enqueue();
        assert.equal(providerStarts,2,'exact replay must not start another model turn');
        assert.deepEqual(session.clearQueue(),{steering:[],followUp:[]});
      } finally {unsubscribe();witness();release();await first;}
    });
  });
}

for (const eventType of ['message_start','message_end']) {
  test(`throwing SDK ${eventType} observer cannot preempt tracked user persistence`, async () => {
    await withSession(async ({session,sm,model}) => {
      let entered,release;
      const active=new Promise(resolve=>{entered=resolve;});
      const held=new Promise(resolve=>{release=resolve;});
      let providerStarts=0, thrown=0, witnessed=0;
      session.agent.streamFunction=async()=>{
        if(++providerStarts===1){entered();await held;}
        return fakeResponse(model);
      };
      const first=session.prompt('first',{inputId:ID_A});await active;
      await session.followUp('second',undefined,ID_B);
      const unsubscribe=session.subscribe(event=>{
        if(event.type===eventType && event.message.inputId===ID_B){
          thrown++;
          throw Error(`observer threw on ${eventType}`);
        }
      });
      const witness=session.subscribe(event=>{
        if(event.type===eventType && event.message.inputId===ID_B) witnessed++;
      });
      try {
        release();await first;
        assert.equal(thrown,1);
        assert.equal(witnessed,1,'other SDK observers still receive the event');
        assert.equal(providerStarts,2);
        assert(sm.getTrackedInput(ID_B));
        assert.equal(records(sm.getSessionFile()).filter(row=>
          row.type==='message'&&row.message.inputId===ID_B).length,1);
        assert(records(sm.getSessionFile()+'.input-proof').some(row=>row.inputId===ID_B));
        await session.followUp('second',undefined,ID_B);
        assert.equal(providerStarts,2);
      } finally {unsubscribe();witness();release();await first;}
    });
  });
}

test('observer containment never hides a tracked-user fsync failure', async () => {
  await withSession(async ({session,sm,model}) => {
    let entered,release;
    const active=new Promise(resolve=>{entered=resolve;});
    const held=new Promise(resolve=>{release=resolve;});
    let providerStarts=0, observerThrows=0, contextReceipts=0, inputReceipts=0;
    session.agent.streamFunction=async()=>{
      if(++providerStarts===1){entered();await held;}
      return fakeResponse(model);
    };
    const first=session.prompt('first',{inputId:ID_A});await active;
    await session.followUp('second',undefined,ID_B);
    const original=sm.flushInputDurably.bind(sm);
    sm.flushInputDurably=(id)=>{
      if(id===ID_B) throw Error('injected second-input fsync failure');
      return original(id);
    };
    const unsubscribe=session.subscribe(event=>{
      if(event.type==='message_end' && event.message.inputId===ID_B){
        observerThrows++;
        throw Error('presentation observer also throws');
      }
      if(event.type==='input_committed' && event.inputId===ID_B) inputReceipts++;
      if(event.type==='context_committed' && event.inputId===ID_B) contextReceipts++;
    });
    try {
      release();await first;
      assert.equal(observerThrows,1);
      assert.equal(providerStarts,1,'the second input never reaches fake provider');
      assert.equal(inputReceipts,0);
      assert.equal(contextReceipts,0);
      assert.equal(sm.getTrackedInput(ID_B)?.message?.inputId,ID_B,
        'an append can remain even though its required fsync failed');
      assert.equal(records(sm.getSessionFile()+'.input-proof').filter(row=>
        row.inputId===ID_B).length,0,'an append is not a live context receipt');
    } finally {
      unsubscribe();sm.flushInputDurably=original;release();await first;
    }
  });
});

test('direct tracked queue metadata failure releases only its fresh unqueued claim', async () => {
  await withSession(async ({session}) => {
    const original=session.agent.steer.bind(session.agent);
    session.agent.steer=()=>{throw Error('core rejected before enqueue');};
    await assert.rejects(session.steer('first',undefined,ID_B),/before enqueue/);
    assert.equal(session._nativeInputClaims.has(ID_B),false);
    assert.deepEqual(session.getSteeringMessages(),[]);
    assert.deepEqual(session.agent.steeringQueue.messages,[]);
    session.agent.steer=original;
    await session.steer('first',undefined,ID_B);
    assert.deepEqual(session.agent.steeringQueue.messages.map(item=>item.inputId),[ID_B]);
    assert.deepEqual(session.clearQueue().steering,['first']);
    assert.equal(session._nativeInputClaims.has(ID_B),false);
  });
});

for (const kind of ['steer','followUp']) {
  test(`${kind}: pre-core metadata failure does not leave a native claim`, async () => {
    await withSession(async ({session}) => {
      // Invalid SDK image input throws while constructing the user message,
      // before either actual core queue can own the tracked ID.
      await assert.rejects(session[kind]('image',1,ID_B),TypeError);
      await assert.rejects(session[kind]('image',1,ID_B),TypeError);
      assert.equal(session._nativeInputClaims.has(ID_B),false);
      assert.deepEqual(session.agent.steeringQueue.messages,[]);
      assert.deepEqual(session.agent.followUpQueue.messages,[]);
      assert.deepEqual(session.getSteeringMessages(),[]);
      assert.deepEqual(session.getFollowUpMessages(),[]);
    });
  });
  test(`${kind}: a post-core exception retains local/core identity until clear`, async () => {
    await withSession(async ({session}) => {
      const core=kind==='steer' ? session.agent.steeringQueue : session.agent.followUpQueue;
      const local=()=>kind==='steer' ? session.getSteeringMessages() : session.getFollowUpMessages();
      const original=session.agent[kind].bind(session.agent);
      session.agent[kind]=(message)=>{
        original(message);
        throw Error('core already enqueued before SDK failure');
      };
      try {
        await assert.rejects(session[kind]('queued',undefined,ID_B),/already enqueued/);
      } finally {session.agent[kind]=original;}
      assert.deepEqual(core.messages.map(item=>item.inputId),[ID_B]);
      assert.deepEqual(local(),['queued']);
      assert.equal(session._nativeInputClaims.has(ID_B),true);
      await session[kind]('queued',undefined,ID_B);
      assert.deepEqual(core.messages.map(item=>item.inputId),[ID_B]);
      assert.deepEqual(session.clearQueue()[kind==='steer'?'steering':'followUp'],['queued']);
      assert.equal(session._nativeInputClaims.has(ID_B),false);
    });
  });
  test(`${kind}: a no-op core enqueue cannot create a display-only ID`, async () => {
    await withSession(async ({session}) => {
      const original=session.agent[kind].bind(session.agent);
      session.agent[kind]=()=>{};
      try {
        await assert.rejects(session[kind]('queued',undefined,ID_B),/Pi core did not queue/);
      } finally {session.agent[kind]=original;}
      assert.equal(session._nativeInputClaims.has(ID_B),false);
      assert.deepEqual(session.getSteeringMessages(),[]);
      assert.deepEqual(session.getFollowUpMessages(),[]);
      await session[kind]('queued',undefined,ID_B);
      const core=kind==='steer' ? session.agent.steeringQueue : session.agent.followUpQueue;
      assert.deepEqual(core.messages.map(item=>item.inputId),[ID_B]);
      session.clearQueue();
    });
  });
}

test('two identical queued follow-ups retain distinct IDs and durable individual context proofs', async () => {
  await withSession(async ({session, model}) => {
    let release;
    const hold = new Promise(resolve=> {release=resolve;});
    let entered;
    const active = new Promise(resolve => {entered=resolve;});
    let sent = 0;
    session.agent.streamFunction = async () => {
      sent++;
      if (sent === 1) {entered(); await hold;}
      return fakeResponse(model);
    };
    const first = session.prompt('first', {inputId: ID_A});
    await active;
    await session.prompt('identical', {inputId: ID_B, streamingBehavior:'followUp'});
    await session.prompt('identical', {inputId: ID_C, streamingBehavior:'followUp'});
    release();
    await first;
    assert.equal(sent, 3);
    const users = records(session.sessionFile)
      .filter(row=>row.type==='message'&&row.message.role==='user');
    assert.deepEqual(users.map(row=>row.message.inputId),[ID_A,ID_B,ID_C]);
    const proofs = records(session.sessionFile+'.input-proof');
    assert(proofs.some(row=>row.inputId===ID_B));
    assert(proofs.some(row=>row.inputId===ID_C));
    assert.equal(session.pendingMessageCount,0);
  });
});

test('a later context prune cannot forge another proof for an old removed input', async () => {
  await withSession(async ({session, model}) => {
    let sent=0;
    session.agent.streamFunction=async () => {sent++; return fakeResponse(model);};
    await session.prompt('first', {inputId:ID_A});
    const original=session.agent.transformContext;
    session.agent.transformContext=async (messages, signal) => {
      const value=original ? await original(messages,signal) : messages;
      return value.filter(m=>m.inputId!==ID_A);
    };
    await session.prompt('second', {inputId:ID_B});
    assert.equal(sent,2);
    const proofs=records(session.sessionFile+'.input-proof');
    assert.deepEqual(proofs.filter(row=>row.requestGeneration===2).map(row=>row.inputId),[ID_B]);
  });
});

test('tracked 429 with default Pi retry enabled has one provider opportunity and no automatic retry', async () => {
  await withSession(async ({session,sm,model,settings}) => {
    assert.equal(settings.getRetryEnabled(),true);
    let providerStarts=0;
    const events=[];
    session.subscribe(event=>{
      if(['context_committed','auto_retry_start','agent_end'].includes(event.type)) events.push(event);
    });
    session.agent.streamFunction=async (_m,_context,options)=>{
      providerStarts++;
      assert.equal(options.maxRetries,0,'tracked transport retry budget must be zero');
      const message={role:'assistant',content:[],api:model.api,provider:model.provider,
        model:model.id,usage:emptyUsage,stopReason:'error',
        errorMessage:'429 Too Many Requests',timestamp:Date.now()};
      return {async *[Symbol.asyncIterator](){yield {type:'done',message};},
        async result(){return message;}};
    };
    await session.prompt('one authorized request',{inputId:ID_A});
    assert.equal(providerStarts,1);
    assert.deepEqual(records(sm.getSessionFile()+'.input-proof').map(row=>row.requestGeneration),[1]);
    assert.deepEqual(events.filter(event=>event.type==='context_committed').map(event=>event.inputId),[ID_A]);
    assert.equal(events.filter(event=>event.type==='auto_retry_start').length,0);
    assert.equal(events.find(event=>event.type==='agent_end')?.willRetry,false);
    assert.equal(sm.getEntries().filter(row=>row.type==='message'&&
      row.message?.role==='assistant').at(-1)?.message.stopReason,'error');
    await session.prompt('one authorized request',{inputId:ID_A});
    assert.equal(providerStarts,1,'same-ID replay cannot retry a failed provider call');
  }, {}, {retry:{baseDelayMs:1,maxRetries:1,provider:{maxRetries:2}}});
});

test('tracked context-overflow provider error never triggers auto-compaction retry', async () => {
  await withSession(async ({session,sm,model}) => {
    let calls=0;
    const events=[];
    session.subscribe(event=>{
      if(['auto_retry_start','compaction_start','context_committed'].includes(event.type)) events.push(event);
    });
    session.agent.streamFunction=async()=>{
      calls++;
      const message={role:'assistant',content:[],api:model.api,provider:model.provider,
        model:model.id,usage:emptyUsage,stopReason:'error',
        errorMessage:'maximum context length exceeded',timestamp:Date.now()};
      return {async *[Symbol.asyncIterator](){yield {type:'done',message};},
        async result(){return message;}};
    };
    await session.prompt('context limit',{inputId:ID_A});
    assert.equal(calls,1);
    assert.equal(events.filter(event=>event.type==='context_committed').length,1);
    assert.equal(events.filter(event=>event.type==='auto_retry_start').length,0);
    assert.equal(events.filter(event=>event.type==='compaction_start').length,0);
    assert.equal(records(sm.getSessionFile()+'.input-proof').length,1);
  }, {}, {compaction:{enabled:true},retry:{baseDelayMs:1,maxRetries:1}});
});

test('untracked Pi retry behavior and configured provider budget remain unchanged', async () => {
  await withSession(async ({session,model,runtime,settings}) => {
    assert.equal(settings.getRetryEnabled(),true);
    const sdkStream=session.agent.streamFunction;
    let calls=0;
    const events=[];
    session.subscribe(event=>{if(event.type==='auto_retry_start') events.push(event);});
    session.agent.streamFunction=async()=>{
      const attempt=++calls;
      const message={role:'assistant',content:[{type:'text',text:attempt===1?'':'ok'}],
        api:model.api,provider:model.provider,model:model.id,usage:emptyUsage,
        stopReason:attempt===1?'error':'stop',
        errorMessage:attempt===1?'429 Too Many Requests':undefined,timestamp:Date.now()};
      return {async *[Symbol.asyncIterator](){yield {type:'done',message};},
        async result(){return message;}};
    };
    await session.prompt('ordinary');
    assert.equal(calls,2);
    assert.equal(events.length,1);
    const providerOptions=[];
    session.agent.streamFunction=async (_m,_context,options)=>{
      providerOptions.push(options.maxRetries);
      return fakeResponse(model);
    };
    await session.prompt('ordinary again');
    await session.prompt('tracked',{inputId:ID_A});
    assert.equal(providerOptions[0],undefined,'untracked core options retain prior behavior');
    assert.equal(providerOptions[1],0,'tracked core transport options override retries');
    // The pinned SDK wrapper forwards the budget to ModelRuntime, not just
    // a fake stream function; no network provider is invoked.
    const original=runtime.streamSimple;
    const applied=[];
    runtime.streamSimple=async (_m,_context,options)=>{
      applied.push(options.maxRetries);
      return fakeResponse(model);
    };
    session.agent.streamFunction=sdkStream;
    try {
      await session.prompt('ordinary SDK wrapper');
      await session.prompt('tracked SDK wrapper',{inputId:ID_B});
      assert.deepEqual(applied,[2,0]);
    } finally {runtime.streamSimple=original;}
  }, {}, {retry:{baseDelayMs:1,maxRetries:1,provider:{maxRetries:2}}});
});

test('tracked truncated tool-call response never auto-continues to a second provider request', async () => {
  await withSession(async ({session,sm,model}) => {
    let providerStarts=0;
    session.agent.streamFunction=async()=>{
      providerStarts++;
      const message={role:'assistant',content:[{type:'toolCall',id:'truncated-1',
        name:'missing_tool',arguments:{}}],api:model.api,provider:model.provider,
        model:model.id,usage:emptyUsage,stopReason:'length',timestamp:Date.now()};
      return {async *[Symbol.asyncIterator](){yield {type:'done',message};},
        async result(){return message;}};
    };
    await session.prompt('truncated tool use',{inputId:ID_A});
    assert.equal(providerStarts,1);
    assert.deepEqual(records(sm.getSessionFile()+'.input-proof').map(row=>row.requestGeneration),[1]);
  });
});

test('a tool turn generates two context generations for one durable input', async () => {
  await withSession(async ({session, model}) => {
    let sent=0;
    session.agent.streamFunction=async () => {
      sent++;
      if(sent!==1) return fakeResponse(model);
      const message={role:'assistant',content:[{type:'toolCall',id:'call-native-1',
        name:'missing_tool',arguments:{}}],api:model.api,provider:model.provider,
        model:model.id,usage:emptyUsage,stopReason:'toolUse',timestamp:Date.now()};
      return {async *[Symbol.asyncIterator]() {yield {type:'done',message};},
        async result() {return message;}};
    };
    await session.prompt('use a tool', {inputId:ID_A});
    assert.equal(sent,2);
    const rows=records(session.sessionFile+'.input-proof');
    assert.deepEqual(rows.map(row=>row.requestGeneration),[1,2]);
    assert(rows.every(row=>row.inputId===ID_A));
    assert.equal(records(session.sessionFile).filter(e=>e.type==='message'&&e.message.inputId===ID_A).length,1);
  });
});

test('nonregular proof journal rejects tracked input before storing private ID or calling provider', async () => {
  await withSession(async ({session}) => {
    mkdirSync(session.sessionFile+'.input-proof');
    let sent=0, committed=0;
    session.subscribe(e=>{if(e.type==='context_committed') committed++;});
    session.agent.streamFunction=async()=>{sent++;throw Error('provider must not run');};
    await assert.rejects(session.prompt('private input', {inputId:ID_C}),/not owner-only/);
    assert.equal(sent,0);
    assert.equal(committed,0);
    assert.equal(session.sessionManager.getTrackedInput(ID_C),undefined);
    assert.equal(session.sessionManager.nativeInputProofAvailable(),false);
  });
});

test('fsync failure never reaches provider and never emits context_committed', async () => {
  await withSession(async ({session}) => {
    let sent = 0;
    let committed = 0;
    session.subscribe(e => { if (e.type === 'context_committed') committed++; });
    session.agent.streamFunction = async () => { sent++; throw Error('provider must not run'); };
    session.sessionManager.flushInputDurably = () => {throw Error('injected fsync failure');};
    await session.prompt('first', {inputId: ID_D});
    assert.equal(sent, 0);
    assert.equal(committed, 0);
  });
});

test('first ordinary Pi turn and later tracked input are both private; old 0644 session is rejected', async () => {
  await withSession(async ({session,sm,model,runtime,settings,loader,cwd,agentDir}) => {
    let sent=0;
    session.agent.streamFunction=async()=>{sent++;return fakeResponse(model);};
    await session.prompt('ordinary untracked input');
    const file=sm.getSessionFile();
    assert.equal(statSync(file).mode & 0o777,0o600);
    assert.equal(statSync(dirname(file)).mode & 0o777,0o700);
    await session.prompt('new private input',{inputId:ID_A});
    assert.equal(statSync(file).mode & 0o777,0o600);
    assert.equal(statSync(file+'.input-proof').mode & 0o777,0o600);
    assert.equal(sent,2);
    chmodSync(file,0o644);
    assert.equal(sm.nativeInputProofAvailable(),false);
    await assert.rejects(session.prompt('must not leak',{inputId:ID_B}),/not owner-only/);
    assert.equal(sent,2);
    assert(!records(file).some(e=>e.message?.inputId===ID_B));
    const unsafeReopen=pi.SessionManager.open(file,dirname(file));
    await assert.rejects(pi.createAgentSession({cwd,agentDir,modelRuntime:runtime,model,
      sessionManager:unsafeReopen,settingsManager:settings,resourceLoader:loader,noTools:'all'}),
    /not owner-only/);
  });
});

test('independent 022-umask/public-root repro keeps earlier untracked session private', async () => {
  const originalUmask = process.umask(0o022);
  try {
    await withSession(async ({root,session,sm,model}) => {
      chmodSync(root,0o755); // Independent repro: public ancestor, no private root assumption.
      session.agent.streamFunction=async()=>fakeResponse(model);
      await session.prompt('untracked synthetic');
      const file=sm.getSessionFile();
      assert.equal(statSync(root).mode & 0o777,0o755);
      assert.equal(statSync(dirname(file)).mode & 0o777,0o700);
      assert.equal(statSync(file).mode & 0o777,0o600);
      await session.prompt('tracked synthetic',{inputId:ID_A});
      assert.equal(statSync(file).mode & 0o777,0o600);
      assert.equal(statSync(file+'.input-proof').mode & 0o777,0o600);
      assert(readFileSync(file,'utf8').includes(ID_A));
    });
  } finally {process.umask(originalUmask);}
});

test('unsafe session directory mode or redirected ancestor refuses tracked IDs before prompt', async () => {
  await withSession(async ({session,sm,root}) => {
    const originalDir=dirname(sm.getSessionFile());
    chmodSync(originalDir,0o755);
    assert.equal(sm.nativeInputProofAvailable(),false);
    await assert.rejects(session.prompt('bad mode',{inputId:ID_A}),/not private/);
    chmodSync(originalDir,0o700);
    const moved=join(root,'private-sessions');
    renameSync(originalDir,moved);
    symlinkSync(moved,originalDir);
    assert.equal(sm.nativeInputProofAvailable(),false);
    await assert.rejects(session.prompt('symlink',{inputId:ID_B}),/not private/);
    assert(!existsSync(sm.getSessionFile()));
  });
});

test('extension handled tracked input fails ACK and clears ghost claim on repeated ID', async () => {
  const extensionFactories=[api=>api.on('input',()=>({action:'handled'}))];
  await withSession(async ({session,sm}) => {
    const acknowledgments=[];
    let sent=0;
    session.agent.streamFunction=async()=>{sent++;throw Error('never call provider');};
    for(let attempt=0;attempt<2;attempt++) {
      await assert.rejects(session.prompt('handled',{inputId:ID_A,source:'rpc',
        preflightResult:value=>acknowledgments.push(value)}),/handled without an LLM context/);
      assert.equal(sm.getTrackedInput(ID_A),undefined);
      assert.equal(session._nativeInputClaims.has(ID_A),false);
    }
    assert.deepEqual(acknowledgments,[false,false]);
    assert.equal(sent,0);
  },{extensionFactories});
});

test('two real concurrent SDK prompts cannot retain a pre-append ghost claim after provisional ACK', async () => {
  await withSession(async ({session,sm,model}) => {
    // Both pass the initial isStreaming preflight while awaiting a real Pi
    // before-agent hook. The second then hits agent.activeRun before append.
    const before=session._extensionRunner.emitBeforeAgentStart.bind(session._extensionRunner);
    let waiting=0, releasePreflight;
    const gate=new Promise(resolve=>{releasePreflight=resolve;});
    session._extensionRunner.emitBeforeAgentStart=async (...args)=>{
      if(++waiting===2) releasePreflight();
      await gate;
      return before(...args);
    };
    let releaseModel;
    const held=new Promise(resolve=>{releaseModel=resolve;});
    let providerStarts=0;
    session.agent.streamFunction=async()=>{
      if(++providerStarts===1) await held;
      return fakeResponse(model);
    };
    const ackA=[],ackB=[];
    const first=session.prompt('first',{inputId:ID_A,preflightResult:ok=>ackA.push(ok)});
    const second=session.prompt('second',{inputId:ID_B,preflightResult:ok=>ackB.push(ok)});
    try {
      await assert.rejects(second,/Agent is already processing/);
      assert.deepEqual(ackB,[true],'the first ACK is only provisional');
      assert.equal(sm.getTrackedInput(ID_B),undefined);
      assert.equal(session._nativeInputClaims.has(ID_B),false);
      await assert.rejects(session.prompt('second',{inputId:ID_B,
        preflightResult:ok=>ackB.push(ok)}),/Agent is already processing/);
      assert.deepEqual(ackB,[true,true],'a concurrent ACK remains provisional until live receipt');
      assert.equal(session._nativeInputClaims.has(ID_B),false,'a rejected replay leaves no ghost');
    } finally {
      releaseModel();
      await first;
    }
    await session.prompt('second',{inputId:ID_B,preflightResult:ok=>ackB.push(ok)});
    assert.deepEqual(ackA,[true]);
    assert.deepEqual(ackB,[true,true,true]);
    assert.equal(providerStarts,2);
    assert(sm.getTrackedInput(ID_B),'the same ID must now be durably delivered');
    assert.equal(records(sm.getSessionFile()).filter(row=>row.message?.inputId===ID_B).length,1);
    await session.prompt('second',{inputId:ID_B});
    assert.equal(providerStarts,2,'exact completed replay must not create a duplicate model turn');
  });
});

test('throwing successful preflight callback releases only its fresh pre-append claim', async () => {
  await withSession(async ({session, sm, model}) => {
    let providerStarts=0;
    session.agent.streamFunction=async()=>{providerStarts++;return fakeResponse(model);};
    const acknowledgments=[];
    await assert.rejects(session.prompt('fresh',{inputId:ID_A,
      preflightResult:ok=>{acknowledgments.push(ok);throw Error('ACK transport failed');}}),
    /ACK transport failed/);
    assert.deepEqual(acknowledgments,[true],'never emit failure after attempted success');
    assert.equal(session._nativeInputClaims.has(ID_A),false);
    assert.equal(sm.getTrackedInput(ID_A),undefined);
    assert.equal(providerStarts,0);
    await session.prompt('fresh',{inputId:ID_A});
    assert.equal(providerStarts,1,'exact same ID must actually reach the provider after rejection');
    assert(sm.getTrackedInput(ID_A));
    await session.prompt('fresh',{inputId:ID_A});
    assert.equal(providerStarts,1,'durably delivered replay stays inert');
  });
});

test('throwing same-ID replay callback cannot release an original running owner', async () => {
  await withSession(async ({session, sm, model}) => {
    const before=session._extensionRunner.emitBeforeAgentStart.bind(session._extensionRunner);
    let entered, release;
    const atHook=new Promise(resolve=>{entered=resolve;});
    const gate=new Promise(resolve=>{release=resolve;});
    session._extensionRunner.emitBeforeAgentStart=async (...args)=>{
      entered();
      await gate;
      return before(...args);
    };
    let providerStarts=0;
    session.agent.streamFunction=async()=>{providerStarts++;return fakeResponse(model);};
    const first=session.prompt('original',{inputId:ID_A});
    await atHook;
    const acknowledgments=[];
    try {
      await assert.rejects(session.prompt('original',{inputId:ID_A,
        preflightResult:ok=>{acknowledgments.push(ok);throw Error('replay ACK failed');}}),
      /replay ACK failed/);
      assert.deepEqual(acknowledgments,[true]);
      assert.equal(session._nativeInputClaims.has(ID_A),true,'original owns the claim');
      assert.equal(sm.getTrackedInput(ID_A),undefined,'original has not yet appended');
    } finally {
      release();
      await first;
    }
    assert.equal(providerStarts,1);
    assert(sm.getTrackedInput(ID_A));
    await session.prompt('original',{inputId:ID_A});
    assert.equal(providerStarts,1,'completed exact replay cannot double-send');
    assert.equal(records(sm.getSessionFile()).filter(row=>row.message?.inputId===ID_A).length,1);
  });
});

test('post-preflight error after user append retains the claim and suppresses duplicate replay', async () => {
  await withSession(async ({session,sm,model}) => {
    let providerStarts=0;
    session.agent.streamFunction=async()=>{providerStarts++;return fakeResponse(model);};
    const run=session._runAgentPrompt.bind(session);
    session._runAgentPrompt=async(...args)=>{
      await run(...args);
      throw Error('injected error after durable user append');
    };
    await assert.rejects(session.prompt('attempt',{inputId:ID_A}),/after durable user append/);
    assert(sm.getTrackedInput(ID_A));
    assert(session._nativeInputClaims.has(ID_A));
    const entries=sm.getEntries().length;
    await session.prompt('attempt',{inputId:ID_A});
    assert.equal(sm.getEntries().length,entries);
    assert.equal(providerStarts,1);
  });
});

for (const restart of [false,true]) {
  test(`failed first proof-directory fsync is retried before another receipt/provider (${restart ? 'reopen' : 'live'})`, async () => {
    await withSession(async ({session,sm,model,runtime,settings,loader,cwd,agentDir}) => {
      let providerStarts=0,firstReceipts=0,dirAttempts=0,proofFileSynced=false;
      const file=sm.getSessionFile(),proof=file+'.input-proof';
      session.agent.streamFunction=async()=>{providerStarts++;return fakeResponse(model);};
      session.subscribe(event=>{if(event.type==='context_committed') firstReceipts++;});
      const original=fs.fsyncSync;
      fs.fsyncSync=(fd)=>{
        const path=fs.readlinkSync(`/proc/self/fd/${fd}`);
        if(path===proof){
          const result=original(fd);
          proofFileSynced=true;
          return result;
        }
        if(path===dirname(proof) && proofFileSynced){
          proofFileSynced=false;
          if(++dirAttempts===1) throw Error('injected first proof-directory fsync failure');
        }
        return original(fd);
      };
      syncBuiltinESMExports();
      let reopened;
      try {
        await session.prompt('first attempt',{inputId:ID_A});
        assert.equal(providerStarts,0,'failed directory fsync blocks provider');
        assert.equal(firstReceipts,0,'a visible proof row is not a live receipt');
        assert.equal(dirAttempts,1);
        assert.deepEqual(records(proof).map(row=>row.requestGeneration),[1]);
        await session.prompt('first attempt',{inputId:ID_A});
        assert.equal(providerStarts,0,'exact replay cannot automatically retry uncertain attempt');
        assert.equal(dirAttempts,1);
        let active=session;
        if(restart){
          const reopenedSm=pi.SessionManager.open(file,dirname(file));
          ({session:reopened}=await pi.createAgentSession({cwd,agentDir,modelRuntime:runtime,model,
            sessionManager:reopenedSm,settingsManager:settings,resourceLoader:loader,noTools:'all'}));
          active=reopened;
          assert.equal(reopened._nativeRequestGeneration,1,'old row only reserves generation');
        }
        const secondReceipts=[];
        active.subscribe(event=>{if(event.type==='context_committed') secondReceipts.push(event);});
        active.agent.streamFunction=async (_m,context)=>{
          providerStarts++;
          assert.deepEqual(context.messages.filter(m=>m.role==='user').map(m=>m.inputId),[ID_A,ID_B]);
          assert.equal(dirAttempts,2,'successful parent fsync precedes provider transport');
          return fakeResponse(model);
        };
        await active.prompt('second distinct explicit attempt',{inputId:ID_B});
        assert.equal(providerStarts,1);
        assert.equal(dirAttempts,2);
        assert.deepEqual(secondReceipts.map(event=>event.inputId),[ID_A,ID_B]);
        assert(secondReceipts.every(event=>event.requestGeneration===2));
        assert.equal(firstReceipts,restart ? 0 : 2,
          'no event for failed generation; live subscriber sees only new generation');
        assert.deepEqual(records(proof).map(row=>row.requestGeneration),[1,2,2]);
      } finally {
        fs.fsyncSync=original;syncBuiltinESMExports();reopened?.dispose();
      }
    });
  });
}

test('full journal line after failed fsync is NOT a recovered context commitment', async () => {
  await withSession(async ({session,sm,model,runtime,settings,loader,cwd,agentDir}) => {
    const observed=[];
    session.subscribe(e=>{if(e.type==='context_committed') observed.push(e);});
    let sent=0;
    session.agent.streamFunction=async()=>{sent++;return fakeResponse(model);};
    const original=fs.fsyncSync;
    fs.fsyncSync=(fd)=>{
      const path=fs.readlinkSync(`/proc/self/fd/${fd}`);
      if(path.endsWith('.input-proof')) throw Error('injected journal fsync failure');
      return original(fd);
    };
    syncBuiltinESMExports();
    try { await session.prompt('attempt',{inputId:ID_A}); }
    finally {fs.fsyncSync=original;syncBuiltinESMExports();}
    assert.equal(sent,0);
    assert.equal(observed.length,0);
    const file=sm.getSessionFile();
    // Exact independent offline repro: ONE complete JSONL row survives failed
    // proof fsync, but is not an emitted or recoverable context receipt.
    const unsynced=records(file+'.input-proof');
    assert.equal(unsynced.length,1);
    assert.equal(unsynced[0].inputId,ID_A);
    const resumed=pi.SessionManager.open(file,dirname(file));
    const {session:reopened}=await pi.createAgentSession({cwd,agentDir,modelRuntime:runtime,
      model,sessionManager:resumed,settingsManager:settings,resourceLoader:loader,noTools:'all'});
    try {
      let replayEvents=0,replayCalls=0;
      reopened.subscribe(e=>{if(e.type==='context_committed') replayEvents++;});
      reopened.agent.streamFunction=async()=>{replayCalls++;throw Error('no recovered replay');};
      assert.equal(resumed.getTrackedInput(ID_A).message.inputId,ID_A);
      assert.equal(reopened._nativeRequestGeneration,1,'the old row only reserves generation');
      assert.equal(replayEvents,0,'reading a journal is never a live fsync event');
      await reopened.prompt('attempt',{inputId:ID_A});
      assert.equal(replayEvents,0);
      assert.equal(replayCalls,0);
    } finally {reopened.dispose();}
  });
});
