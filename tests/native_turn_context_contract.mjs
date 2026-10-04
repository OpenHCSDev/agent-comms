/** SDK source contract. No provider input, credentials or public session. */
import assert from 'node:assert/strict';
import {mkdirSync, writeFileSync, readFileSync, existsSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {spawn} from 'node:child_process';
import {constructNativeConditions,applyNativeCondition,
    transformBoundedNativeCondition,armBoundedNativeCondition,armInstalledNativeCondition} from './retained_native_conditions.mjs';

const [pkg, suppliedRoot] = process.argv.slice(2);
const root=resolve(suppliedRoot);
globalThis.fetch = async () => {throw new Error('SOURCE_CONTRACT_FORBIDS_NETWORK');};
if (process.argv.includes('--system-source-builder-child')) {
    // Compare the original builder, not a second implementation of its prompt.
    // Both imports borrow the same declared PI_PACKAGE_DIR for documentation
    // paths. The source artifact and its hashes remain distinct.
    const {buildSystemPrompt}=await import(pathToFileURL(join(pkg,'dist/core/system-prompt.js')));
    let input=''; for await (const bytes of process.stdin) input+=bytes;
    const cases=JSON.parse(input);
    const output=JSON.stringify(cases.map(item=>({name:item.name,
        prompt:buildSystemPrompt(item.options)})))+'\n';
    await new Promise((resolve,reject)=>process.stdout.write(output,
        error=>error ? reject(error) : resolve()));
    process.exit(0);
}
if (process.argv.includes('--system-source-spans')) process.env.PI_PACKAGE_DIR=resolve(pkg);
const pi = await import(pathToFileURL(join(pkg,'dist/index.js')));
const {TurnContext,NativeInputClaim} = await import(pathToFileURL(join(pkg,'dist/core/turn-context.js')));
const {estimateTokens} = await import(pathToFileURL(join(pkg,'dist/core/compaction/compaction.js')));
if (process.argv.includes('--system-source-spans')) {
    console.log(JSON.stringify(await systemSourceSpans(),null,2));
    process.exit(0);
}
if (process.argv.includes('--comparison-child')) {
    // Each SDK keeps its original committed import boundary. The child borrows
    // this fixture's selected file/coordinates and acquired JSON values only.
    let input=''; for await (const bytes of process.stdin) input+=bytes;
    const request=JSON.parse(input);
    const runtime=await pi.ModelRuntime.create({authPath:join(request.agentDir,'auth.json'),
        modelsPath:join(request.agentDir,'models.json'),modelsStorePath:join(request.agentDir,'models-store.json')});
    const settings=pi.SettingsManager.inMemory({compaction:{enabled:false},retry:{enabled:false}});
    const loader=new pi.DefaultResourceLoader({cwd:request.cwd,agentDir:request.agentDir,settingsManager:settings});
    await loader.reload();
    const manager=pi.SessionManager.open(request.session_file);
    const {session}=await pi.createAgentSession({cwd:request.cwd,agentDir:request.agentDir,
        modelRuntime:runtime,settingsManager:settings,sessionManager:manager,resourceLoader:loader});
    try {
        const entries=request.entries===undefined ? undefined : request.entries.map(id=>manager.getEntry(id));
        const output=JSON.stringify(await measureCapture(TurnContext,session,request.context,entries));
        await new Promise((resolve,reject)=>process.stdout.write(output+'\n',
            error=>error ? reject(error) : resolve()));
    } finally {session.dispose();manager.entryStore.close();}
    process.exit(0);
}
const cwd=join(root,'project'),agentDir=join(root,'config');
mkdirSync(cwd,{recursive:true,mode:0o700});mkdirSync(agentDir,{mode:0o700});
writeFileSync(join(cwd,'AGENTS.md'),'Original project instruction π.\n');
mkdirSync(join(cwd,'.pi'));writeFileSync(join(cwd,'.pi','APPEND_SYSTEM.md'),'Original appended instruction.\n');
const runtime=await pi.ModelRuntime.create({authPath:join(agentDir,'auth.json'),
    modelsPath:join(agentDir,'models.json'),modelsStorePath:join(agentDir,'models-store.json')});
const settings=pi.SettingsManager.inMemory({compaction:{enabled:false},retry:{enabled:false}});
const loader=new pi.DefaultResourceLoader({cwd,agentDir,settingsManager:settings});
await loader.reload();
const manager=pi.SessionManager.create(cwd,join(root,'sessions'));
const old=manager.appendMessage({role:'user',content:'Original prior question',timestamp:1});
manager.appendMessage({role:'assistant',content:[{type:'text',text:'Original prior answer'}],
    api:'openai-completions',provider:'source-only',model:'source-only',timestamp:2,
    stopReason:'stop',usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,
    cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});
const kept=manager.appendMessage({role:'user',content:[
    {type:'text',text:'Original kept question π 🙂'},
    {type:'image',data:'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==',mimeType:'image/png'},
],timestamp:3});
const compaction=manager.appendCompaction('Original source summary.',kept,100);
const recordedEntries=[kept];
if (process.argv.includes('--recorded-readers'))
    recordedEntries.push(manager.appendMessage({role:'user',content:'Distinct recorded child.',timestamp:4}));
if (process.argv.includes('--retained-history'))
    manager.appendMessage({role:'user',content:'Representative retained native history. '.repeat(3000),timestamp:4});
manager.appendCustomMessageEntry('source-contract','Original injected delivery',true);
const {session}=await pi.createAgentSession({cwd,agentDir,modelRuntime:runtime,
    settingsManager:settings,sessionManager:manager,resourceLoader:loader});
try {
    const before=readFileSync(manager.getSessionFile());
    const provider={systemPrompt:session.systemPrompt,
        messages:await session.agent.convertToLlm(Array.from(session.storedContext.messages(session.agent))),
        tools:session.agent.state.tools};
    const captured=await TurnContext.capture(session,provider);
    const full=captured.full();
    if (process.argv.includes('--configured-condition-rpc')) {
        await observeConfiguredPrompt(session,pkg,root,compaction);
    } else if ((process.argv.includes('--condition-agent-loop') || process.argv.includes('--installed-condition-loop'))) {
        console.log(JSON.stringify(await observeConditionLoop(session,pkg,root,compaction,before)));
    } else {
    // End validation for capture ownership: compare the unchanged public
    // contract against the original immutable SDK on the same acquired values.
    const comparisonIndex=process.argv.indexOf('--comparison-package');
    const comparisonPackage=comparisonIndex<0 ? undefined : resolve(process.argv[comparisonIndex+1]);
    const compare=async(selected,context,entries)=>{
        const child=spawn(process.execPath,[new URL(import.meta.url).pathname,
            comparisonPackage,root,'--comparison-child'],{stdio:['pipe','pipe','pipe']});
        let output='',error='';
        child.stdout.on('data',bytes=>{output+=bytes;});
        child.stderr.on('data',bytes=>{error+=bytes;});
        const exited=new Promise((resolve,reject)=>{
            child.on('error',reject); child.on('close',code=>resolve(code));
        });
        child.stdin.end(JSON.stringify({cwd,agentDir,session_file:selected.sessionFile,
            context,entries:entries?.map(entry=>entry.id)}));
        const code=await exited;
        assert.equal(code,0,error);
        const original=JSON.parse(output), current=await measureCapture(TurnContext,selected,context,entries);
        const originalFull=original.full, currentFull=current.full;
        // The system file reference honestly names each artifact. Its original
        // bytes and every other measurement/source coordinate must be identical.
        const reference=originalFull.segments[0].provenance[1];
        const currentReference=currentFull.segments[0].provenance[1];
        assert.equal(reference.path,join(comparisonPackage,'dist/core/system-prompt.js'));
        assert.equal(reference.sha256,currentReference.sha256);
        originalFull.segments[0].provenance[1]={...reference,path:currentReference.path};
        assert.deepEqual(currentFull,originalFull);
        assert.deepEqual(current.render,JSON.parse(JSON.stringify(context)));
        const expected=original.observation;
        expected.segments[0].provenance[1]={...reference,path:currentReference.path};
        expected.values[0].provenance[1]={...reference,path:currentReference.path};
        assert.deepEqual(current.observation,expected);
        assert.deepEqual(current.manifest,
            (({values,...manifest})=>manifest)(expected));
        return {messages:context.messages.length,
            original_ms:original.elapsed_ms,current_ms:current.elapsed_ms,
            original_journal_encodings:original.journal_encodings,
            current_journal_encodings:current.journal_encodings,
            exact_manifest_and_values:true,render_identical:true};
    };
    let captureComparison;
    if (comparisonPackage) {
        const repeated={...provider,messages:[...provider.messages,...provider.messages,
            {role:'user',content:'Authored transformed SDK value',timestamp:999},
            {role:'user',content:'Second authored transformed SDK value',timestamp:1000}]};
        captureComparison={repeated_and_transformed:await compare(session,repeated)};
        const savedIndex=process.argv.indexOf('--saved-source');
        if (savedIndex>=0) {
            const saved=resolve(process.argv[savedIndex+1]);
            const originalHash=hashFile(saved);
            const fork=pi.SessionManager.forkFrom(saved,cwd,join(root,'saved-sdk-fork'));
            let acquired;
            try {
                ({session:acquired}=await pi.createAgentSession({cwd,agentDir,modelRuntime:runtime,
                    settingsManager:settings,sessionManager:fork,resourceLoader:loader}));
                const selectedHash=hashFile(fork.getSessionFile());
                const sourceView=await TurnContext.fullSource(acquired);
                const entries=Array.from(fork.entryStore.uncompactedMetadata(fork.getLeafId()),
                    metadata=>fork.entryStore.get(metadata.id));
                captureComparison.retained=await compare(acquired,sourceView.render(),entries);
                assert.equal(hashFile(fork.getSessionFile()),selectedHash);
                assert.equal(hashFile(saved),originalHash);
                captureComparison.retained.source_bytes=readFileSync(saved).length;
                captureComparison.retained.source_sha256=originalHash;
                captureComparison.retained.source_unchanged=true;
            } finally {acquired?.dispose();fork.entryStore.close();}
        }
    }
    const original=manager.getEntry(kept).message;
    const text=original.content[0].text, images=original.content.slice(1);
    const hash=value=>createHash('sha256').update(value).digest('hex');
    const request={kind:'prompt',text,images,streamingBehavior:null,expandPromptTemplates:true,source:'rpc'};
    const digest=hash(`pi-input-request-v1\n${JSON.stringify(request)}`);
    const journal={kind:'journal',path:manager.getSessionFile(),entries:[kept]};
    const coordinates={kind:'source_input',provenance:[journal],offset:0,
        length:Buffer.byteLength(text),sha256:hash(text),images:[0]};
    const claim=NativeInputClaim.capture(digest,request,[coordinates]);
    assert.equal(claim.digest,digest);
    const preview=full.segments[0].provenance.find(source=>source.kind==='preview');
    assert(preview && !('request_generation' in preview));
    const [measured]=claim.observe(original,preview,journal);
    assert.equal(measured.tokens,estimateTokens(original));
    assert.equal(measured.sha256,hash(JSON.stringify(original.content)));
    for (const invalid of [{offset:-1},{length:coordinates.length+1},{sha256:'0'.repeat(64)},
        {images:[1]},{provenance:[]}])
        assert.throws(()=>NativeInputClaim.capture(digest,request,[{...coordinates,...invalid}]));
    assert.equal(claim.observe({...original,content:'Extension transformed input'},preview,journal)[0].kind,
        'transformed_input');
    const changedImage={...original,content:[original.content[0],{...images[0],
        data:'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=='}]};
    assert.equal(claim.observe(changedImage,preview,journal)[0].kind,'transformed_input');
    assert.equal(JSON.stringify(captured.render()),JSON.stringify(provider));
    assert.deepEqual(readFileSync(manager.getSessionFile()),before);
    assert(full.segments.some(s=>s.kind==='compaction_summary'));
    assert(full.segments.some(s=>s.kind==='injection_message'));
    assert(full.segments.some(s=>s.kind==='tool_catalog'));
    const system=full.segments.find(s=>s.kind==='system_layer');
    assert(system.provenance.some(s=>s.path===join(cwd,'AGENTS.md')));
    assert(system.provenance.some(s=>s.path===join(cwd,'.pi','APPEND_SYSTEM.md')));
    assert(!JSON.stringify(captured.manifest()).includes('Original source summary'));
    assert.deepEqual((await TurnContext.next(session)).render(),provider);
    let mixedCapture;
    if (process.argv.includes('--recorded-readers')) {
        // Recorded roots and mixed original parts borrow one real SDK projection.
        // Counters observe this acquisition; all data comes from the original store.
        const selected=await TurnContext.project(session,recordedEntries);
        const recorded=selected.manifest().segments.find(segment=>segment.kind==='transcript');
        assert(recorded?.contributors.length);
        const project=TurnContext.project;
        let projections=0;
        TurnContext.project=async function(...args) {
            projections++;
            return project.apply(this,args);
        };
        try {
            const whole=await TurnContext.recordedSegment(session,full.identity,recordedEntries,recorded,[]);
            assert.equal(projections,1);
            assert.equal(whole.full().segments.length,1);
            assert.equal(whole.full().segments[0].sha256,recorded.sha256);
            projections=0;
            const child=recorded.contributors[0];
            assert.notEqual(child.sha256,recorded.sha256);
            const childEntries=child.provenance.find(source=>source.kind==='journal').entries;
            const childRead=await TurnContext.recordedSegment(session,full.identity,childEntries,child,[]);
            assert.equal(projections,1);
            assert.equal(childRead.full().segments[0].sha256,child.sha256);
            assert.notEqual(childRead.full().segments[0].sha256,recorded.sha256);
            await assert.rejects(TurnContext.recordedSegment(session,full.identity,childEntries,recorded,
                recorded.contributors),/projected bytes differ/);
            projections=0;
            let transformed=0;
            const mixedProvider={...provider,messages:provider.messages.map(message=>{
                if (message.role!=='user' || message.content!=='Distinct recorded child.') return message;
                transformed++;
                return {...message,content:'Authored transformed SDK part.'};
            })};
            assert.equal(transformed,1);
            mixedCapture=await TurnContext.capture(session,mixedProvider);
            const mixed=mixedCapture.manifest().segments.find(segment=>
                segment.kind==='transcript' && segment.contributors.length>1);
            const originalDigests=new Set(recorded.contributors.map(part=>part.sha256));
            const parts=mixed.contributors.filter(part=>originalDigests.has(part.sha256));
            assert.equal(parts.length,1);
            assert.notEqual(mixed.sha256,recorded.sha256);
            const resolved=await TurnContext.recordedSegment(session,full.identity,recordedEntries,mixed,parts);
            assert.equal(projections,1);
            assert.deepEqual(resolved.full().segments.map(segment=>segment.sha256),parts.map(part=>part.sha256));
            await assert.rejects(TurnContext.recordedSegment(session,full.identity,recordedEntries,mixed,[]),/projected bytes differ/);
            await assert.rejects(TurnContext.recordedSegment(session,full.identity,recordedEntries,mixed,
                [{...parts[0],sha256:'f'.repeat(64)}]),/projected bytes differ/);
            await assert.rejects(TurnContext.recordedSegment(session,{...full.identity,sessionId:'another'},
                recordedEntries,recorded,[]),/another native session/);
        } finally {TurnContext.project=project;}
        assert.deepEqual(readFileSync(manager.getSessionFile()),before);
    }
    const conditions = process.argv.includes('--source-projections')
        ? await constructNativeConditions(session, pkg) : undefined;
    if (conditions) {
        const fullSource=JSON.stringify(conditions['full-context'].context);
        const recent=JSON.stringify(conditions['recent-only'].context);
        assert(fullSource.includes('Original prior question'));
        assert(!fullSource.includes('Original source summary.'));
        assert(!recent.includes('Original prior question'));
        assert(!recent.includes('Original source summary.'));
        assert(recent.includes('Original kept question'));
        assert.deepEqual(conditions['task-memory'].context, provider);
        for (const name of ['full-context','recent-only','task-memory']) {
            const observed=conditions[name];
            assert.deepEqual(observed.identity, full.identity);
            assert(observed.manifest.segments.every(segment=>segment.provenance.some(source=>source.kind==='preview')));
        }
        assert.equal(conditions.bounded.evaluated, false);
        // This authored SDK construction control is not a recorded model
        // response. Production measurement input comes only from the original
        // corroborated RecordedNativeCheckpoint.condition_source consumer.
        const source={evaluated:true,session:full.identity,native_entry_id:compaction,
            summary:'Authored uncombined narrative only.',
            checkpoint_session:full.identity,
            source:{kind:'file',path:new URL(import.meta.url).pathname,
                sha256:hash(readFileSync(new URL(import.meta.url)))}};
        const constructed=await constructNativeConditions(session,pkg,source);
        const bounded=constructed.bounded;
        assert.equal(bounded.evaluated,true);
        assert(JSON.stringify(bounded.context).includes(source.summary));
        assert(!JSON.stringify(bounded.context).includes('Original source summary.'));
        assert(!JSON.stringify(bounded.context).includes('Original prior question'));
        assert(JSON.stringify(bounded.context).includes('Original kept question'));
        assert.deepEqual(bounded.context.systemPrompt,provider.systemPrompt);
        assert.deepEqual(bounded.context.tools,provider.tools);
        assert.deepEqual(bounded.identity,full.identity);
        assert.deepEqual(bounded.narrative_source,source.source);
        assert.deepEqual((await constructNativeConditions(session,pkg,
            {evaluated:false,reason:'Original narrative unavailable'})).bounded,
            {evaluated:false,reason:'Original narrative unavailable'});
        await assert.rejects(constructNativeConditions(session,pkg,
            {...source,session:{...source.session,sessionId:'another'}}),/another original native session/);
        await assert.rejects(constructNativeConditions(session,pkg,
            {...source,native_entry_id:old}),/not the selected native compaction/);
        const {SessionContext}=await import(pathToFileURL(join(pkg,'dist/core/session-context.js')));
        // Every intervention uses the same raw SDK installation boundary.
        // A converted preview is never installed as AgentMessages.
        for (const selected of Object.values(constructed)) {
            const applied=await applyNativeCondition(session,pkg,selected);
            assert.deepEqual(session.agent.state.messages,selected.agent_messages);
            assert.deepEqual((await TurnContext.next(session)).render(),selected.context);
            assert.deepEqual(applied.source_witness,selected.source_witness);
            SessionContext.restore(session);
            assert.deepEqual((await TurnContext.next(session)).render(),provider);
        }
        assert.equal(bounded.agent_messages[0].role,'compactionSummary');
        assert.equal(bounded.agent_messages[0].summary,source.summary);
        assert.deepEqual(bounded.checkpoint_session,full.identity);
        await assert.rejects(applyNativeCondition(session,pkg,
            {evaluated:false,reason:'Original narrative unavailable'}),/evaluated SDK construction/);
        await assert.rejects(applyNativeCondition(session,pkg,{...bounded,
            agent_messages:[{role:'user',content:'Changed construction.',timestamp:500}]}),
            /construction changed before installation/);
        await assert.rejects(applyNativeCondition(session,pkg,{...bounded,
            source_witness:{...bounded.source_witness,sessionId:'another'}}),
            /selection changed since condition construction/);
        // Budget belongs to the current selected model, not the preview.
        const model=session.model;
        const {BudgetAdmissionError}=await import(pathToFileURL(join(pkg,
            'node_modules/@earendil-works/pi-ai/dist/api/agent-comms-context-budget.js')));
        session.agent.state.model={...model,contextWindow:1};
        try {
            await assert.rejects(applyNativeCondition(session,pkg,bounded),BudgetAdmissionError);
            assert.deepEqual((await TurnContext.next(session)).render(),provider);
        } finally {session.agent.state.model=model;}
        const selectedContext=session.storedContext;
        const {CompactionContext}=await import(pathToFileURL(join(pkg,'dist/core/session-context.js')));
        session.storedContext=new CompactionContext(manager);
        try {
            await assert.rejects(applyNativeCondition(session,pkg,bounded),/did not admit/);
            assert.deepEqual((await TurnContext.next(session)).render(),provider);
        } finally {session.storedContext=selectedContext;}
        const fresh={role:'user',content:'Distinct new input preserved.',timestamp:500};
        const raw=[...session.agent.state.messages,fresh];
        const transformed=await transformBoundedNativeCondition(session,pkg,source,raw);
        assert.equal(transformed[0].summary,source.summary);
        assert.deepEqual(transformed.slice(1),raw.slice(1));
        assert.deepEqual(session.agent.state.messages,raw.slice(0,-1));
        await assert.rejects(transformBoundedNativeCondition(session,pkg,source,raw.slice(1)),/not unique/);
        const originalTransform=async messages=>[...messages,fresh];
        session.agent.transformContext=originalTransform;
        const restore=armBoundedNativeCondition(session,pkg,source,
            transformBoundedNativeCondition,
            join(root,'application-observation.jsonl'),'authored-control');
        const hooked=await session.agent.transformContext(raw.slice(0,-1));
        assert.deepEqual(hooked.at(-1),fresh);
        assert.equal(hooked[0].summary,source.summary);
        const applicationRecords=readFileSync(join(root,'application-observation.jsonl'),'utf8')
            .trim().split('\n').map(line=>JSON.parse(line));
        const appliedRecords=applicationRecords.filter(row=>row.stage==='bounded-transform-applied');
        assert.equal(appliedRecords.length,1);
        assert.equal(appliedRecords[0].agent_messages_sha256,
            createHash('sha256').update(JSON.stringify(hooked)).digest('hex'));
        restore();
        assert.equal(session.agent.transformContext,originalTransform);
        assert.deepEqual(readFileSync(manager.getSessionFile()),before);
        // Changed selected sources refuse instead of installing stale bytes.
        // This append affects only this authored SDK fixture, never a donor.
        const preparedMessages=session.agent.state.messages;
        manager.appendCustomMessageEntry('source-contract','Distinct later source',true);
        await assert.rejects(applyNativeCondition(session,pkg,bounded),/selection changed since/);
        assert.equal(session.agent.state.messages,preparedMessages);
    }
    console.log(JSON.stringify({scope:'actual-sdk-source-contract',provider_calls:0,
        provider_bytes_identical:true,journal_bytes_unchanged:!conditions,
        ...(conditions ? {condition_constructions_installed:4,
            construction_scope:'Authored SDK selections/installation/refusals; not submitted inputs or model/provider baselines',
            original_source_preserved_through_install_and_restore:true,
            distinct_authored_append_refused:true} : {}),
        original_contribution_tokens:measured.tokens,invalid_coordinates_refused:5,
        transformation_observed_without_input_rejection:true,preview_not_recorded:true,
        kinds:full.segments.map(s=>s.kind),session_file:manager.getSessionFile(),capture_comparison:captureComparison,
        ...(!process.argv.includes('--summary-only') ? {full,conditions} : {}),
        ...(process.argv.includes('--recorded-readers') && !process.argv.includes('--summary-only') ? {
            recorded_observation:captured.observation('authored-sdk-source-request'),
            mixed_observation:mixedCapture.observation('authored-sdk-mixed-request'),
            mixed_full:mixedCapture.full(),
            recorded_reader_scope:'Authored SDK capture and root/child resolution; no onContextReady model request',
            root_single_projection:true,child_uses_original_coordinates:true,
            mixed_single_projection:true,missing_or_other_session_refused:true} : {}),
        condition_construction_scope:conditions ? 'Authored SDK four-condition installation and canonical restore; no input or captured model baseline' : undefined}));
    }
} finally {session.dispose();manager.entryStore.close();}

async function observeConfiguredPrompt(session,pkg,root,compaction) {
    // This authored SDK host runs the original stdin RPC, prompt, input claim,
    // persistence and onContextReady publication. Only the transport is controlled;
    // it does not qualify a configured provider, user lease or study arm.
    const {AgentSessionRuntime}=await import(pathToFileURL(join(pkg,'dist/core/agent-session-runtime.js')));
    const {runRpcMode}=await import(pathToFileURL(join(pkg,'dist/modes/rpc/rpc-mode.js')));
    const {AssistantMessageEventStream}=await import(pathToFileURL(join(pkg,
        'node_modules/@earendil-works/pi-ai/dist/utils/event-stream.js')));
    assert(session.model,'Original SDK selection required');
    const identity={sessionId:session.sessionId,sessionFile:session.sessionFile};
    writeFileSync(join(root,'condition-source.json'),JSON.stringify({evaluated:true,
        session:identity,checkpoint_session:identity,native_entry_id:compaction,
        summary:'Authored uncombined narrative only.',source:{kind:'file',
            path:new URL(import.meta.url).pathname,sha256:hashFile(new URL(import.meta.url))}}),{mode:0o600});
    const promptDescriptor=Object.getOwnPropertyDescriptor(session,'prompt');
    const beforeContext=session.storedContext;
    const beforeDescriptor=Object.getOwnPropertyDescriptor(beforeContext,'beforeInput');
    const transform=session.agent.transformContext;
    let streams=0;
    if (!process.argv.includes('--preflight-refusal'))
        await session.modelRuntime.setRuntimeApiKey(session.model.provider,'authored-unused-transport-key');
    session.agent.streamFunction=(model,context,options)=>{
        streams++;
        const stream=new AssistantMessageEventStream();
        const message={role:'assistant',content:[{type:'text',text:'Authored RPC terminal.'}],
            api:model.api,provider:model.provider,model:model.id,timestamp:Date.now(),
            stopReason:'stop',usage:{input:0,output:0,cacheRead:0,cacheWrite:0,
                totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
        if (process.argv.includes('--abort-stream')) {
            options.signal.addEventListener('abort',()=>stream.push({type:'error',reason:'aborted',
                error:{...message,stopReason:'aborted'}}),{once:true});
        } else stream.push({type:'done',reason:'stop',message});
        return stream;
    };
    const host=new AgentSessionRuntime(session,{cwd:session.sessionManager.getCwd(),
        agentDir:join(root,'config')},async()=>{
            throw new Error('This authored prompt control does not replace sessions');
        });
    session.subscribe(event=>{
        if(event.type==='agent_settled') {
            assert.deepEqual(Object.getOwnPropertyDescriptor(session,'prompt'),promptDescriptor);
            assert.deepEqual(Object.getOwnPropertyDescriptor(beforeContext,'beforeInput'),beforeDescriptor);
            assert.equal(session.agent.transformContext,transform);
        }
    });
    process.on('exit',()=>{
        assert.deepEqual(Object.getOwnPropertyDescriptor(session,'prompt'),promptDescriptor);
        assert.deepEqual(Object.getOwnPropertyDescriptor(beforeContext,'beforeInput'),beforeDescriptor);
        assert.equal(session.agent.transformContext,transform);
        writeFileSync(join(root,'prompt-exit.json'),JSON.stringify({provider_calls:0,
            authored_RPC_prompts:true,controlled_streams:streams,hook_restored:true,
            native_identity:identity}),{mode:0o600});
    });
    await runRpcMode(host);
}

async function observeConditionLoop(session,pkg,root,compaction,before) {
    // Authored plumbing control: the original loop/SDK/inspector execute, while
    // an SDK event stream supplies one terminal. No Agent.prompt, input proof,
    // enrolled input, provider transport or model-selection change is made.
    const {runAgentLoop}=await import(pathToFileURL(join(pkg,
        'node_modules/@earendil-works/pi-agent-core/dist/agent-loop.js')));
    const {AssistantMessageEventStream}=await import(pathToFileURL(join(pkg,
        'node_modules/@earendil-works/pi-ai/dist/utils/event-stream.js')));
    assert(session.model,'Existing SDK selected model required');
    const identity={sessionId:session.sessionId,sessionFile:session.sessionFile};
    const source={evaluated:true,session:identity,checkpoint_session:identity,
        native_entry_id:compaction,summary:'Authored uncombined narrative only.',
        source:{kind:'file',path:new URL(import.meta.url).pathname,
            sha256:hashFile(new URL(import.meta.url))}};
    const observation=join(root,'application-observation.jsonl');
    const installed=process.argv.includes('--installed-condition-loop');
    const originalTransform=session.agent.transformContext;
    const originalMessages=Array.from(session.storedContext.messages(session.agent));
    const constructions=installed ? await constructNativeConditions(session,pkg,source) : {};
    const selections=installed ? Object.values(constructions) : [undefined];
    const results=[];
    try {
    for (const [index,construction] of selections.entries()) {
    if (construction) await applyNativeCondition(session,pkg,construction);
    const inputId='d'.repeat(31)+index.toString(16);
    const restore=construction ? armInstalledNativeCondition(session,construction,observation,inputId)
        : armBoundedNativeCondition(session,pkg,source,transformBoundedNativeCondition,observation,inputId);
    const events=[],progress=[];
    let manifest,transportCalls=0;
    try {
        const messages=[...session.agent.state.messages,
            {role:'user',content:'Authored SDK input context, not a submitted prompt.',
                timestamp:500,inputId}];
        const output=await runAgentLoop([],{systemPrompt:session.systemPrompt,
            messages,tools:session.agent.state.tools},{model:session.model,
            sessionId:session.sessionId,
            transformContext:session.agent.transformContext,
            convertToLlm:session.agent.convertToLlm,
            onRequestProgress:record=>progress.push(record),
            onContextReady:async(context,requestId)=>{
                // Authored source descriptor only, not a minted native claim.
                // The original TurnContext publishes the original request ID.
                const view=await TurnContext.capture(session,context,{
                    request_generation:1,context_digest:createHash('sha256')
                        .update(`pi-assembled-context-v1\n${JSON.stringify(context)}`).digest('hex')});
                manifest=view.manifest(requestId);
            }},event=>{events.push(event.type);},undefined,
            (model,context,options)=>{
                transportCalls++;
                assert.equal(options.sessionId,identity.sessionId);
                const message={role:'assistant',content:[{type:'text',text:'Authored terminal.'}],
                    api:model.api,provider:model.provider,model:model.id,timestamp:501,
                    stopReason:'stop',usage:{input:0,output:0,cacheRead:0,cacheWrite:0,
                        totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
                const stream=new AssistantMessageEventStream();
                stream.push({type:'done',reason:'stop',message});
                return stream;
            });
        assert.equal(transportCalls,1);
        assert.equal(output.length,1);
        assert.equal(output[0].stopReason,'stop');
        assert.equal(events.at(-1),'agent_end');
        assert(progress.every(row=>row.requestId===manifest.requestId
            && row.sessionId===identity.sessionId && row.inputId===inputId));
    } finally {restore();}
    assert.equal(session.agent.transformContext,originalTransform);
    assert.deepEqual(readFileSync(session.sessionFile),before);
    results.push({condition:construction?.condition, input_id:inputId,manifest,events,progress,
        controlled_streams:transportCalls});
    }
    } finally {session.storedContext.install(session.agent,originalMessages);}
    assert.deepEqual(session.agent.state.messages,originalMessages);
    return {scope:'Authored original SDK installation/agent-loop/inspector/onContextReady plumbing',
        provider_calls:0,submitted_prompts:0,
        controlled_streams:results.reduce((total,result)=>total+result.controlled_streams,0),
        native_claim_minted:false,journal_bytes_unchanged:true,hook_restored:true,
        installed_conditions:installed ? results.length : 0,canonical_messages_restored:true,
        identity,results};
}

function hashFile(path) {
    return createHash('sha256').update(readFileSync(path)).digest('hex');
}

async function systemSourceSpans() {
    const comparisonIndex=process.argv.indexOf('--comparison-package');
    assert(comparisonIndex>=0,'W1 byte parity requires the original builder package');
    const comparisonPackage=resolve(process.argv[comparisonIndex+1]);
    const cwd=join(root,'project'), agentDir=join(root,'config');
    const agents=join(cwd,'AGENTS.md'), append=join(cwd,'.pi','APPEND_SYSTEM.md');
    const custom=join(root,'custom-system.md'), skill=join(root,'skills','audit','SKILL.md');
    for (const path of [cwd,agentDir,join(cwd,'.pi'),join(root,'skills','audit')])
        mkdirSync(path,{recursive:true,mode:0o700});
    const authored=new Map([
        [agents,'\uFEFFProject rule π 🙂.\n'],
        [append,'\uFEFFAppend rule café.\n'],
        [custom,'\uFEFFCustom system rule λ.\n'],
        [skill,'---\nname: audit\ndescription: Audit original source π\n---\nPRIVATE_SKILL_BODY_NOT_LOADED\n'],
    ]);
    for (const [path,content] of authored) writeFileSync(path,content,{mode:0o600});
    const runtime=await pi.ModelRuntime.create({authPath:join(agentDir,'auth.json'),
        modelsPath:join(agentDir,'models.json'),modelsStorePath:join(agentDir,'models-store.json')});
    const cases=[
        {name:'default'},
        {name:'custom-file',systemPrompt:custom},
        {name:'unchanged-overrides',systemPrompt:custom,
            systemPromptOverride:value=>value,appendSystemPromptOverride:values=>values},
        {name:'changed-system',systemPrompt:custom,
            systemPromptOverride:value=>value+'Extension-owned replacement.\n'},
        {name:'changed-append',appendSystemPromptOverride:values=>
            [...values,'Extension-owned appended wording.\n']},
        {name:'literal-system',systemPrompt:'Literal source has no file authority.\n'},
    ];
    const parity=[], receipts=[];
    const hash=value=>createHash('sha256').update(value).digest('hex');
    for (const {name,...overrides} of cases) {
        const settings=pi.SettingsManager.inMemory({compaction:{enabled:false},retry:{enabled:false}});
        const loader=new pi.DefaultResourceLoader({cwd,agentDir,settingsManager:settings,
            noExtensions:true,noPromptTemplates:true,noThemes:true,
            additionalSkillPaths:[join(root,'skills','audit')],...overrides});
        await loader.reload();
        const manager=pi.SessionManager.create(cwd,join(root,'sessions',name));
        let session;
        try {
            ({session}=await pi.createAgentSession({cwd,agentDir,modelRuntime:runtime,
                settingsManager:settings,sessionManager:manager,resourceLoader:loader}));
            // A new SDK session has an original in-memory header until its
            // first append. Inspection must neither flush it nor create input.
            const saved=manager.getSessionFile();
            const before=existsSync(saved) ? readFileSync(saved) : null;
            const context={systemPrompt:session.systemPrompt,messages:[],tools:session.agent.state.tools};
            const captured=await TurnContext.capture(session,context);
            const system=captured.full().segments.find(segment=>segment.kind==='system_layer');
            const bytes=Buffer.from(system.content);
            assert.equal(system.content,session.systemPrompt);
            // Whole manifests measure the original JSON representation;
            // contribution coordinates address the emitted raw UTF8 text.
            const encoded=JSON.stringify(system.content);
            assert.equal(system.sha256,hash(encoded));
            assert.equal(system.utf8_bytes,Buffer.byteLength(encoded));
            let offset=0;
            for (const span of system.source_spans) {
                assert.equal(span.kind,'system_layer');
                assert.equal(span.offset,offset,'assembly spans must cover their original ordered writes');
                assert.equal(span.sha256,hash(bytes.subarray(span.offset,span.offset+span.length)));
                offset+=span.length;
            }
            assert.equal(offset,bytes.length);
            const fromFile=path=>system.source_spans.filter(span=>
                span.provenance.some(source=>source.kind==='file' && source.path===path));
            for (const path of [agents,...(name==='changed-append' ? [] : [append]),
                ...(['custom-file','unchanged-overrides'].includes(name) ? [custom] : [])]) {
                const [span]=fromFile(path);
                assert(span,`${name}: missing original file span ${path}`);
                assert.equal(span.provenance.find(source=>source.kind==='file').sha256,hashFile(path));
                assert.equal(bytes.subarray(span.offset,span.offset+span.length).toString(),
                    authored.get(path).replace(/^\uFEFF/,''),'BOM removal must not corrupt UTF8 coordinates');
            }
            if (name==='changed-system') assert.equal(fromFile(custom).length,0);
            if (name==='changed-append') assert.equal(fromFile(append).length,0);
            const skillSpan=system.source_spans.find(span=>span.provenance.some(source=>
                source.kind==='resource' && source.path===skill));
            assert(skillSpan,'SDK skill metadata must retain its own resource source');
            const skillText=bytes.subarray(skillSpan.offset,skillSpan.offset+skillSpan.length).toString();
            assert(skillText.includes('Audit original source π'));
            assert(!system.content.includes('PRIVATE_SKILL_BODY_NOT_LOADED'));
            assert.equal(fromFile(skill).length,0,'skill metadata is not skill-file wording');
            const requestId=`source-only-${name}`;
            const observation=captured.observation(requestId);
            assert.equal(observation.requestId,requestId);
            const manifest=observation.segments.find(segment=>segment.kind==='system_layer');
            const value=observation.values.find(segment=>segment.kind==='system_layer');
            assert.deepEqual(manifest.source_spans,system.source_spans);
            assert.deepEqual(value.source_spans,system.source_spans);
            assert.equal(value.content,system.content);
            assert.deepEqual(captured.render(),context);
            // A transformed full extension value cannot inherit file attribution.
            const transformed=await TurnContext.capture(session,{...context,
                systemPrompt:context.systemPrompt+'Transformed full value.\n'});
            const changed=transformed.full().segments.find(segment=>segment.kind==='system_layer');
            assert(changed.source_spans.every(span=>
                span.provenance.every(source=>source.kind==='unattributed')));
            assert.deepEqual(existsSync(saved) ? readFileSync(saved) : null,before);
            parity.push({name,options:{...session._baseSystemPromptOptions,systemSources:undefined},
                prompt:session.systemPrompt});
            receipts.push({name,utf8_bytes:bytes.length,spans:system.source_spans.length,
                captured_values_and_request_id:true,journal_unchanged:true});
        } finally {session?.dispose();manager.entryStore.close();}
    }
    const child=spawn(process.execPath,[new URL(import.meta.url).pathname,
        comparisonPackage,root,'--system-source-builder-child'],{stdio:['pipe','pipe','pipe']});
    let output='',error='';
    child.stdout.on('data',bytes=>{output+=bytes;});
    child.stderr.on('data',bytes=>{error+=bytes;});
    const exited=new Promise((resolve,reject)=>{
        child.on('error',reject);child.on('close',code=>resolve(code));
    });
    child.stdin.end(JSON.stringify(parity.map(({name,options})=>({name,options}))));
    assert.equal(await exited,0,error);
    assert.deepEqual(JSON.parse(output),parity.map(({name,prompt})=>({name,prompt})),
        'default/custom byte parity must be exact against the original SDK builder');
    for (const [path,content] of authored) assert.equal(readFileSync(path).toString(),content);
    return {scope:'Original SDK source-only assembly/capture; no committed provider request',
        provider_requests:0,native_inputs:0,original_builder_byte_parity:true,cases:receipts};
}

async function measureCapture(declaration,session,context,entries) {
    const stringify=JSON.stringify;
    let journalEncodings=0;
    JSON.stringify=function(value,...args) {
        if (value?.kind==='journal') journalEncodings++;
        return stringify.call(this,value,...args);
    };
    const started=performance.now();
    let view,elapsed;
    try {
        view=await declaration.capture(session,context,undefined,entries);
        elapsed=performance.now()-started;
    } finally {JSON.stringify=stringify;}
    // The original protocol boundary owns JSON representation, including omitted
    // undefined JS properties. No alternate import loader or cached source.
    return JSON.parse(JSON.stringify({full:view.full(),render:view.render(),
        observation:view.observation('same-acquired-source'),manifest:view.manifest('same-acquired-source'),
        elapsed_ms:elapsed,journal_encodings:journalEncodings}));
}
