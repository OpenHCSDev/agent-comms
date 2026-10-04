/** SDK source contract. No provider input, credentials or public session. */
import assert from 'node:assert/strict';
import {mkdirSync, writeFileSync, readFileSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {spawn} from 'node:child_process';
import {constructNativeConditions,applyNativeCondition,
    transformBoundedNativeCondition,armBoundedNativeCondition,armInstalledNativeCondition} from './retained_native_conditions.mjs';

const [pkg, suppliedRoot] = process.argv.slice(2);
const root=resolve(suppliedRoot);
globalThis.fetch = async () => {throw new Error('SOURCE_CONTRACT_FORBIDS_NETWORK');};
const pi = await import(pathToFileURL(join(pkg,'dist/index.js')));
const {TurnContext,NativeInputClaim} = await import(pathToFileURL(join(pkg,'dist/core/turn-context.js')));
const {estimateTokens} = await import(pathToFileURL(join(pkg,'dist/core/compaction/compaction.js')));
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
    if ((process.argv.includes('--condition-agent-loop') || process.argv.includes('--installed-condition-loop'))) {
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
            assert.deepEqual(observed.manifest.identity, full.identity);
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
        assert.deepEqual(bounded.manifest.identity,full.identity);
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
