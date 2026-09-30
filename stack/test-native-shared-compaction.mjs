// Existing saved native source + actual compiled compaction and SDK events.
// Controlled provider only: no network, credentials, session append or input replay.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync, mkdirSync, rmSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [packageDir, savedSource, mode = 'success'] = process.argv.slice(2);
globalThis.fetch = () => { throw new Error('Network prohibited in controlled acceptance'); };
const load = file => import(pathToFileURL(resolve(packageDir, file)).href);
const { SessionManager } = await load('dist/core/session-manager.js');
const { prepareCompaction, compact } = await load('dist/core/compaction/compaction.js');
const { AssistantMessageEventStream } = await load('node_modules/@earendil-works/pi-ai/dist/utils/event-stream.js');
const model = { provider: 'openai-codex', id: 'gpt-6.1-sol', contextWindow: 272000,
    maxTokens: 128000, reasoning: true };
const settings = { reserveTokens: 16384, keepRecentTokens: 20000 };
const usage = { input: 3, output: 2, reasoning: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 5,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
const digest = () => createHash('sha256').update(readFileSync(savedSource)).digest('hex');
const before = digest();
let manager;
let nestedRoot;
if (mode === 'nested') {
    nestedRoot = resolve(process.env.AGENT_COMMS_SESSION_INDEX_DIR, '../nested-source');
    mkdirSync(nestedRoot, { recursive: true, mode: 0o700 });
    manager = SessionManager.create(nestedRoot, resolve(nestedRoot, 'sessions'));
    const assistant = text => ({ role: 'assistant', content: [{ type: 'text', text }],
        provider: model.provider, model: model.id, api: 'openai-codex-responses',
        stopReason: 'stop', timestamp: 1, usage });
    manager.appendMessage({ role: 'user', content: 'history '.repeat(80000), timestamp: 1 });
    manager.appendMessage(assistant('earlier work'));
    manager.appendMessage({ role: 'user', content: 'current prefix '.repeat(40000), timestamp: 2 });
    for (let i = 0; i < 5; i++) manager.appendMessage(assistant('retained suffix '.repeat(2000)));
} else manager = SessionManager.open(savedSource);
const measuredSource = manager.getSessionFile();
const measuredSourceBytes = readFileSync(measuredSource);
const measuredSourceDigest = createHash('sha256').update(measuredSourceBytes).digest('hex');
const prepareStarted = performance.now();
const preparation = prepareCompaction(manager.entryStore, settings, model, manager.getLeafId());
const preparationMs = performance.now() - prepareStarted;
assert.equal(preparation.isSplitTurn, true);
const requests = [], progress = [], chunks = [];
const controller = new AbortController();
const started = performance.now();
let active = 0, peak = 0, mapIndex = 0;
const stream = async (_model, context, options) => {
    const prompt = context.messages[0].content[0].text;
    const kind = prompt.includes('This is the PREFIX of a turn') ? 'current-turn'
        : prompt.includes('<segment-summary') ? 'synthesis' : 'map';
    const index = kind === 'map' ? mapIndex++ : 0;
    assert.equal(options.maxTokens, 4096);
    assert.equal(options.reasoning, 'high');
    assert.ok(prompt.includes('SHARED_SOURCE_ACCEPTANCE'));
    const request = { kind, index, startMs: performance.now() - started };
    requests.push(request);
    active++; peak = Math.max(peak, active);
    const events = new AssistantMessageEventStream();
    const text = kind === 'map' ? `MAP_${index}` : kind === 'synthesis' ? 'HISTORY_COMPLETE' : 'CURRENT_TURN_COMPLETE';
    const result = { role: 'assistant', content: [{ type: 'text', text }], stopReason: 'stop', usage };
    if (kind === 'synthesis') {
        const order = [...prompt.matchAll(/MAP_(\d+)/g)].map(match => Number(match[1]));
        assert.deepEqual(order, Array.from({ length: mapIndex }, (_, i) => i));
    }
    void (async () => {
        events.push({ type: 'start', partial: result });
        const delay = kind === 'map' ? [200, 236, 250, 266, 130][index] ?? 50
            : kind === 'synthesis' ? 250 : 172;
        let stopped = false;
        await new Promise(resolveWait => {
            const timer = setTimeout(resolveWait, delay);
            const stop = () => { stopped = true; clearTimeout(timer); resolveWait(); };
            if (options.signal?.aborted) stop();
            else options.signal?.addEventListener('abort', stop, { once: true });
            request.detach = () => options.signal?.removeEventListener('abort', stop);
        });
        request.detach(); delete request.detach;
        request.finishMs = performance.now() - started;
        active--;
        if (stopped) {
            request.aborted = true;
            events.push({ type: 'error', reason: 'aborted', error: { ...result, stopReason: 'aborted' } });
        } else if (mode === 'failure' && kind === 'map' && index === 0) {
            events.push({ type: 'error', reason: 'error', error: { ...result,
                stopReason: 'error', errorMessage: 'ORIGINAL_SOURCE_FAILURE' } });
        } else {
            events.push({ type: 'thinking_delta', delta: 'planning', partial: result });
            events.push({ type: 'text_delta', delta: text, partial: result });
            events.push({ type: 'done', reason: 'stop', message: result });
        }
    })();
    if (mode === 'cancel' && requests.length === 4)
        setTimeout(() => controller.abort(new Error('EXPLICIT_OWNER_CANCEL')), 30);
    return events;
};
const response = compact(preparation, model, undefined, undefined, 'SHARED_SOURCE_ACCEPTANCE',
    controller.signal, 'high', stream, {}, { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } }, {
        onSummaryStart: source => progress.push(source),
        onSummaryProgress: source => progress.push(source),
        onSummaryText: (text, source) => { progress.push(source); chunks.push(text); },
        onSummaryResponse: (_usage, source) => { if (source) progress.push(source); },
    }, 'controlled-shared-compaction');
if (mode === 'success' || mode === 'baseline' || mode === 'nested') {
    const result = await response;
    assert.ok(result.summary.indexOf('HISTORY_COMPLETE') < result.summary.indexOf('CURRENT_TURN_COMPLETE'));
    assert.equal(result.usage.totalTokens, requests.length * usage.totalTokens);
    assert.equal(progress.at(-1).sourceBytesDone, progress.at(-1).sourceBytesTotal);
    assert.ok(progress.every((s, i) => s.sourceBytesDone >= (progress[i - 1]?.sourceBytesDone ?? 0)));
    assert.ok(progress.every(s => s.startedAtMs === progress[0].startedAtMs && s.observedAtMs >= s.startedAtMs));
    assert.ok(chunks.includes('CURRENT_TURN_COMPLETE') && chunks.includes('HISTORY_COMPLETE'));
    const current = requests.find(r => r.kind === 'current-turn');
    const history = requests.find(r => r.kind === 'synthesis');
    if (process.env.AGENT_COMMS_COMPACTION_POLICY?.includes('serial')) assert.equal(peak, 1);
    else if (mode !== 'baseline') {
        assert.equal(peak, 4);
        assert.ok(current.startMs < history.finishMs, 'current source must not await history completion');
    }
} else {
    await assert.rejects(response, mode === 'failure' ? /ORIGINAL_SOURCE_FAILURE/ : /EXPLICIT_OWNER_CANCEL|aborted/);
    assert.ok(requests.some(r => r.aborted));
    assert.ok(requests.length <= 4, 'failed or cancelled work must not admit queued sources');
}
assert.equal(active, 0, 'native source execution joins every provider request');
assert.equal(digest(), before, 'saved source bytes remain unchanged; no original input replay');
assert.equal(createHash('sha256').update(readFileSync(measuredSource)).digest('hex'), measuredSourceDigest);
assert.equal(readdirSync(process.env.AGENT_COMMS_SESSION_INDEX_DIR).filter(name => name.startsWith('summary-')).length, 0);
console.log(JSON.stringify({ mode, preparationMs, sourceBytes: measuredSourceBytes.length,
    sourceSelectedBytes: progress[0].sourceBytesTotal, peak, requests,
    providerSpanMs: Math.max(...requests.map(r => r.finishMs)) - Math.min(...requests.map(r => r.startMs)),
    observedProgress: progress.length, streamedChunks: chunks.length, sourceUnchanged: true,
    ownedProviderRequestsJoined: true }));
if (nestedRoot) { manager.entryStore.close(); rmSync(nestedRoot, { recursive: true }); }
