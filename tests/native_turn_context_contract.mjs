/** SDK source contract. No provider input, credentials or public session. */
import assert from 'node:assert/strict';
import {mkdirSync, writeFileSync, readFileSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const [pkg, suppliedRoot] = process.argv.slice(2);
const root=resolve(suppliedRoot);
globalThis.fetch = async () => {throw new Error('SOURCE_CONTRACT_FORBIDS_NETWORK');};
const pi = await import(pathToFileURL(join(pkg,'dist/index.js')));
const {TurnContext} = await import(pathToFileURL(join(pkg,'dist/core/turn-context.js')));
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
    {type:'text',text:'Original kept question'},
    {type:'image',data:'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==',mimeType:'image/png'},
],timestamp:3});
manager.appendCompaction('Original source summary.',kept,100);
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
    console.log(JSON.stringify({scope:'actual-sdk-source-contract',provider_calls:0,
        provider_bytes_identical:true,journal_bytes_unchanged:true,
        kinds:full.segments.map(s=>s.kind),session_file:manager.getSessionFile(),full}));
} finally {session.dispose();}
