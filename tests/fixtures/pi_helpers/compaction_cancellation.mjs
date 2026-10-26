import assert from 'node:assert/strict';
import {mkdirSync} from 'node:fs';
import {pathToFileURL} from 'node:url';

const [root, cwd, mode] = process.argv.slice(2);
const load = name => import(pathToFileURL(`${root}/dist/core/${name}.js`));
const {createAgentSession} = await load('sdk');
const {SessionManager} = await load('session-manager');
const {SettingsManager} = await load('settings-manager');
const {DefaultResourceLoader} = await load('resource-loader');
const {ModelRuntime} = await load('model-runtime');
const {AuthStorage} = await load('auth-storage');
const {NativeProof} = await load('native-proof-journal');
const {estimateTokens} = await load('compaction/compaction');
mkdirSync(cwd, {recursive:true, mode:0o700});
const runtime = await ModelRuntime.create({credentials:AuthStorage.inMemory(), modelsPath:null,
    refreshOnCreate:false, allowModelNetwork:false});
runtime.registerProvider('local-control', {api:'openai-completions', apiKey:'fixture',
    baseUrl:'http://127.0.0.1:1', models:[{id:'fixture', name:'fixture', reasoning:false,
        input:['text'], cost:{input:0, output:0, cacheRead:0, cacheWrite:0},
        contextWindow:128000, maxTokens:4096}]});
const model = runtime.getModel('local-control', 'fixture');
const settings = SettingsManager.inMemory({retry:{enabled:false}});
const loader = new DefaultResourceLoader({cwd, agentDir:cwd, settingsManager:settings,
    noExtensions:true, noSkills:true, noPromptTemplates:true, noThemes:true,
    noContextFiles:true, systemPrompt:'Controlled cancellation fixture.'});
await loader.reload();
const manager = SessionManager.create(cwd, `${cwd}/sessions`);
const usage = {input:mode === 'overflow' ? 1 : 300001, output:1, cacheRead:0, cacheWrite:0, totalTokens:mode === 'overflow' ? 2 : 300002,
    cost:{input:0, output:0, cacheRead:0, cacheWrite:0, total:0}};
for (let i = 0; i < 5; i++) {
    manager.appendMessage({role:'user', content:'history '.repeat(mode === 'overflow' ? 4000 : 40000), timestamp:i*2});
    manager.appendMessage({role:'assistant', content:[{type:'text', text:'recorded'}],
        api:model.api, provider:model.provider, model:model.id,
        stopReason:'stop', timestamp:i*2+1, usage});
}
const {session} = await createAgentSession({cwd, agentDir:cwd, modelRuntime:runtime, model,
    thinkingLevel:'off', sessionManager:manager, settingsManager:settings,
    resourceLoader:loader, tools:[],
    nativeInputProof:NativeProof.fromPath(`${cwd}/proof.sqlite`, manager.getSessionId())});
const events = [];
session.subscribe(event => events.push(event));
let enteredResolve;
const entered = new Promise(resolve => enteredResolve = resolve);
const requests = [];
let hold = true;
session.agent.streamFunction = (_model, context, options) => {
    const summary = context.systemPrompt.includes('context summarization assistant');
    requests.push({summary, context});
    let release;
    const gate = new Promise(resolve => release = resolve);
    const held = summary && hold;
    if (!held || options.signal.aborted) release();
    else options.signal.addEventListener('abort', release, {once:true});
    // Return a successful late response after cancellation: the owner must fence
    // it even if the transport finishes concurrently with the abort signal.
    const overflow = mode === 'overflow' && requests.length === 1 && !summary;
    const response = {role:'assistant', content:[{type:'text',
        text:held ? 'ABANDONED_SUMMARY' : summary ? 'VALID_SUMMARY' : 'FRESH_REPLY'}],
        api:model.api, provider:model.provider, model:model.id, stopReason:overflow ? 'error' : 'stop',
        ...(overflow ? {errorMessage:'maximum context length exceeded'} : {}),
        timestamp:20, usage:{...usage, input:10, totalTokens:11}};
    const stream = (async function* () {
        if (held) enteredResolve();
        await gate;
        yield overflow ? {type:'error', reason:'error', error:response}
            : {type:'done', reason:'stop', message:response};
    })();
    stream.result = async () => { await gate; return response; };
    return stream;
};
const original = '1'.repeat(32), queued = '2'.repeat(32), fresh = '3'.repeat(32);
const operation = mode === 'manual' ? session.compact() :
    session.prompt('ORIGINAL_PENDING_INPUT', {inputId:original, expandPromptTemplates:false});
const completed = operation.then(() => ({ok:true}), error => ({error}));
await entered;
await session.steer('ACCEPTED_QUEUED_CORRECTION', undefined, queued);
assert.equal(manager.getTrackedInput(queued), undefined, 'queue is not model consumption');
assert.throws(() => session.interruptSteering([queued]), /Wait for compaction/);
const start = performance.now();
await session.abort();
assert.ok(performance.now() - start < 1000, 'abort waited for summary completion');
assert.ok((await completed).error, 'cancelled operation continued');
assert.equal(session.isIdle, true);
assert.equal(requests.length, mode === 'overflow' ? 2 : 1, 'cancelled summary triggered another request');
assert.equal(manager.entryStore.latest(manager.getLeafId(), 'compaction'), undefined);
if (mode === 'overflow') assert.ok(manager.getTrackedInput(original));
else assert.equal(manager.getTrackedInput(original), undefined);
assert.equal(manager.getTrackedInput(queued), undefined);
assert.equal(session.agent.steeringQueue.messages.filter(m => m.inputId === queued).length, 1);
const ends = events.filter(event => event.type === 'compaction_end');
assert.equal(ends.length, 1);
assert.equal(ends[0].aborted, true);

hold = false;
await session.prompt('FRESH_RESUME_INPUT', {inputId:fresh, expandPromptTemplates:false});
const prompts = requests.filter(request => !request.summary);
assert.equal(prompts.length, mode === 'overflow' ? 2 : 1);
const context = prompts.at(-1).context;
assert.equal(context.messages.filter(message => message.inputId === queued).length, 1);
assert.equal(context.messages.filter(message => message.inputId === fresh).length, 1);
assert.ok(context.messages.filter(message => message.inputId === original).length <= 1);
assert.ok(context.messages.reduce((tokens, message) => tokens + estimateTokens(message), 0) < 300000);
assert.ok(!JSON.stringify(context).includes('ABANDONED_SUMMARY'));
assert.ok(manager.getTrackedInput(queued));
assert.equal(session.agent.steeringQueue.messages.length, 0);
session.dispose();
console.log(`${mode}: cancellation and fresh bounded continuation passed`);
