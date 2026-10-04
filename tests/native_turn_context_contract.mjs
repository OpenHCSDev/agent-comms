/** SDK source contract. No provider input, credentials or public session. */
import assert from 'node:assert/strict';
import {mkdirSync, writeFileSync, readFileSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {constructNativeConditions,applyBoundedNativeCondition,
    transformBoundedNativeCondition,armBoundedNativeCondition} from './retained_native_conditions.mjs';

const [pkg, suppliedRoot] = process.argv.slice(2);
const root=resolve(suppliedRoot);
globalThis.fetch = async () => {throw new Error('SOURCE_CONTRACT_FORBIDS_NETWORK');};
const pi = await import(pathToFileURL(join(pkg,'dist/index.js')));
const {TurnContext,NativeInputClaim} = await import(pathToFileURL(join(pkg,'dist/core/turn-context.js')));
const {estimateTokens} = await import(pathToFileURL(join(pkg,'dist/core/compaction/compaction.js')));
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
    // End validation for capture ownership: compare the unchanged public
    // contract against the original immutable SDK on the same acquired values.
    const comparisonIndex=process.argv.indexOf('--comparison-package');
    const comparisonPackage=comparisonIndex<0 ? undefined : resolve(process.argv[comparisonIndex+1]);
    const baseline=comparisonPackage ? (await import(pathToFileURL(join(
        comparisonPackage,'dist/core/turn-context.js')))).TurnContext : undefined;
    const compare=async(selected,context,entries)=>{
        const measure=async declaration=>{
            const stringify=JSON.stringify;
            let journalEncodings=0;
            JSON.stringify=function(value,...args) {
                if (value?.kind==='journal') journalEncodings++;
                return stringify.call(this,value,...args);
            };
            const started=performance.now();
            try {
                const view=await declaration.capture(selected,context,undefined,entries);
                return {view,elapsed_ms:performance.now()-started,journal_encodings:journalEncodings};
            } finally {JSON.stringify=stringify;}
        };
        const original=await measure(baseline), current=await measure(TurnContext);
        const originalFull=original.view.full(), currentFull=current.view.full();
        // The system file reference honestly names each artifact. Its original
        // bytes and every other measurement/source coordinate must be identical.
        const reference=originalFull.segments[0].provenance[1];
        const currentReference=currentFull.segments[0].provenance[1];
        assert.equal(reference.path,join(comparisonPackage,'dist/core/system-prompt.js'));
        assert.equal(reference.sha256,currentReference.sha256);
        originalFull.segments[0].provenance[1]={...reference,path:currentReference.path};
        assert.deepEqual(currentFull,originalFull);
        assert.deepEqual(current.view.render(),context);
        const expected=original.view.observation('same-acquired-source');
        expected.segments[0].provenance[1]={...reference,path:currentReference.path};
        expected.values[0].provenance[1]={...reference,path:currentReference.path};
        assert.deepEqual(current.view.observation('same-acquired-source'),expected);
        assert.deepEqual(current.view.manifest('same-acquired-source'),
            (({values,...manifest})=>manifest)(expected));
        return {messages:context.messages.length,
            original_ms:original.elapsed_ms,current_ms:current.elapsed_ms,
            original_journal_encodings:original.journal_encodings,
            current_journal_encodings:current.journal_encodings,
            exact_manifest_and_values:true,render_identical:true};
    };
    let captureComparison;
    if (baseline) {
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
        const applied=await applyBoundedNativeCondition(session,pkg,source);
        // SDK installation consumes AgentMessages, never converted LLM messages.
        assert.equal(session.agent.state.messages[0].role,'compactionSummary');
        assert.equal(session.agent.state.messages[0].summary,source.summary);
        assert.deepEqual((await TurnContext.next(session)).render(),bounded.context);
        assert.deepEqual(applied.context,bounded.context);
        assert.deepEqual(applied.checkpoint_session,full.identity);
        await assert.rejects(applyBoundedNativeCondition(session,pkg,
            {evaluated:false,reason:'Original narrative unavailable'}),/eligible narrative/);
        const {SessionContext}=await import(pathToFileURL(join(pkg,'dist/core/session-context.js')));
        SessionContext.restore(session);
        assert.deepEqual((await TurnContext.next(session)).render(),provider);
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
        restore();
        assert.equal(session.agent.transformContext,originalTransform);
        assert.deepEqual(readFileSync(manager.getSessionFile()),before);
    }
    console.log(JSON.stringify({scope:'actual-sdk-source-contract',provider_calls:0,
        provider_bytes_identical:true,journal_bytes_unchanged:true,
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
        bounded_construction_scope:conditions ? 'Authored SDK construction/application control; raw SDK installation and canonical restore; no input or captured model baseline' : undefined}));
} finally {session.dispose();}

function hashFile(path) {
    return createHash('sha256').update(readFileSync(path)).digest('hex');
}
