// Actual prepared compaction/SDK streaming contracts, without provider or native owner startup.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const manifest = readFileSync(resolve(import.meta.dirname, 'pi-native.sha256'));
const build = createHash('sha256').update(manifest).digest('hex').slice(0, 16);
const packageDir = process.env.PI_NATIVE_PACKAGE_DIR ?? resolve(import.meta.dirname,
    `.pi-native-${build}/node_modules/@earendil-works/pi-coding-agent`);
const { generateSummaryWithUsage } = await import(pathToFileURL(resolve(packageDir,
    'dist/core/compaction/compaction.js')).href);
const { AssistantMessageEventStream } = await import(pathToFileURL(resolve(packageDir,
    'node_modules/@earendil-works/pi-ai/dist/utils/event-stream.js')).href);
const { CompactionPolicy } = await import(pathToFileURL(resolve(packageDir,
    'dist/core/compaction/agent-comms-policy.js')).href);
const model = { provider: 'openai-codex', id: 'selected', contextWindow: 272000,
    maxTokens: 128000, reasoning: true };
const reserve = 16384;
const usage = { input: 1, output: 2, reasoning: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 3,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const receipts = [];

for (const body of ['retained short current turn', 'retained history '.repeat(45000)]) {
    const starts = [], observations = [], chunks = [], responses = [], budgets = [];
    const stream = async (_model, _context, options) => {
        budgets.push(options.maxTokens);
        const result = { role: 'assistant', content: [{ type: 'text', text: 'summary' }],
            stopReason: 'stop', usage };
        const events = new AssistantMessageEventStream();
        void (async () => {
            events.push({ type: 'start', partial: result });
            await delay(10);
            events.push({ type: 'thinking_delta', delta: 'planning', partial: result });
            await delay(10);
            events.push({ type: 'text_delta', delta: 'summary', partial: result });
            await delay(10);
            events.push({ type: 'done', reason: 'stop', message: result });
        })();
        return events;
    };
    const result = await generateSummaryWithUsage(
        [{ role: 'user', content: [{ type: 'text', text: body }], timestamp: 1 }],
        model, reserve, undefined, undefined, undefined, undefined, undefined, 'low', stream,
        {}, { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } }, {
            onSummaryStart: source => starts.push(source),
            onSummaryProgress: source => observations.push(source),
            onSummaryText: (text, source) => chunks.push({ text, source }),
            onSummaryResponse: (_usage, source) => responses.push(source),
        }, undefined);
    const snapshots = [...starts, ...observations, ...chunks.map(c => c.source), ...responses];
    assert.equal(result.text, 'summary');
    assert.ok(snapshots.every(s => Number.isSafeInteger(s.startedAtMs) &&
        s.startedAtMs === snapshots[0].startedAtMs && s.observedAtMs >= s.startedAtMs));
    assert.ok(observations.some(s => s.observedAtMs > observations[0].observedAtMs),
        'thinking heartbeats must query the native clock, not a spread/frozen snapshot');
    assert.ok(observations.every(s => s.sourceBytesDone <= s.sourceBytesTotal));
    assert.equal(responses.at(-1).sourceBytesDone, responses.at(-1).sourceBytesTotal);
    assert.equal(observations[0].sourceBytesDone, 0, 'input is not processed before provider work');
    assert.ok(chunks.length > 0);
    const policy = new CompactionPolicy();
    assert.ok(budgets.every(b => b === policy.summaryTokens(model,
        policy.inputBytes(model, reserve), reserve)), 'all summary requests use the same policy');
    receipts.push({ sourceBytes: Buffer.byteLength(body), calls: budgets.length,
        budget: budgets[0], progressEvents: observations.length, streamedChunks: chunks.length,
        elapsedMs: responses.at(-1).observedAtMs - responses[0].startedAtMs });
}
console.log(JSON.stringify({ preparedNative: packageDir, cases: receipts }));
