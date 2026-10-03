/** SDK source contract. No provider input, credentials or public session. */
import assert from 'node:assert/strict';
import {mkdirSync, writeFileSync, readFileSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {constructNativeConditions} from './retained_native_conditions.mjs';

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
manager.appendCompaction('Original source summary.',kept,100);
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
        assert.deepEqual(readFileSync(manager.getSessionFile()),before);
    }
    console.log(JSON.stringify({scope:'actual-sdk-source-contract',provider_calls:0,
        provider_bytes_identical:true,journal_bytes_unchanged:true,
        original_contribution_tokens:measured.tokens,invalid_coordinates_refused:5,
        transformation_observed_without_input_rejection:true,preview_not_recorded:true,
        kinds:full.segments.map(s=>s.kind),session_file:manager.getSessionFile(),full,conditions}));
} finally {session.dispose();}
