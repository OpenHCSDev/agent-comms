// Credential-free proof that a long history cannot become one oversized summary request.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const manifest = readFileSync(resolve(import.meta.dirname, 'pi-native.sha256'));
const buildId = createHash('sha256').update(manifest).digest('hex').slice(0, 16);
const path = resolve(import.meta.dirname, `.pi-native-${buildId}/node_modules/@earendil-works/pi-coding-agent/dist/core/compaction/compaction.js`);
const { generateSummaryWithUsage, prepareCompaction, compact } = await import(pathToFileURL(path).href);
const usage = {
  input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};
async function summarize(body, previousSummary, instructions, contextWindow = 128000, growFirst = false) {
  const requests = [];
  const progress = [];
  const stream = async (_model, context) => ({
    result: async () => {
      const prompt = context.messages[0].content[0].text;
      requests.push(prompt);
      assert.ok(Buffer.byteLength(prompt, 'utf8') <= (contextWindow - 16384) * 0.75,
        `oversized compaction request: ${Buffer.byteLength(prompt, 'utf8')} bytes`);
      return { stopReason: 'stop', content: [{ type: 'text', text: growFirst && requests.length === 1 ? "large summary ".repeat(3000) : `summary ${requests.length}` }], usage };
    },
  });
  const result = await generateSummaryWithUsage(
    [{ role: 'user', content: [{ type: 'text', text: body }], timestamp: Date.now() }],
    { provider: 'openrouter', id: 'fake', contextWindow, maxTokens: 8192, reasoning: false },
    16384, 'local-fixture', {}, undefined, instructions, previousSummary, undefined,
    stream, {}, { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } }, { onSummaryResponse: (_usage, item) => progress.push(item) }, undefined,
  );
  assert.equal(result.usage.totalTokens, requests.length * 2);
  assert.equal(progress.length, requests.length);
  assert.ok(progress.every((item, i) => item.sourceBytesTotal === progress[0].sourceBytesTotal &&
    item.sourceBytesDone >= (progress[i - 1]?.sourceBytesDone ?? 0) && item.sourceBytesDone <= item.sourceBytesTotal));
  assert.equal(progress.at(-1).sourceBytesDone, progress.at(-1).sourceBytesTotal);
  if (growFirst) {
    const shrinkIndex = progress.findIndex(item => item.summaryPhase === 'shrink');
    assert.ok(shrinkIndex > 0);
    assert.equal(progress[shrinkIndex].sourceBytesDone, progress[shrinkIndex - 1].sourceBytesDone,
      'shrinking the rolling summary must not count source bytes twice');
  }
  return requests;
}

const small = await summarize('short history', undefined, undefined);
assert.equal(small.length, 1);

let emptyCalls = 0;
await assert.rejects(() => generateSummaryWithUsage(
  [{ role: 'user', content: [{ type: 'text', text: 'history '.repeat(62500) }], timestamp: 1 }],
  { provider: 'openrouter', id: 'fake', contextWindow: 128000, maxTokens: 8192 },
  16384, 'local-fixture', {}, undefined, undefined, undefined, undefined,
  async () => ({ result: async () => {
    emptyCalls++;
    return { stopReason: 'stop', content: [{ type: 'text', text: ' \n' }], usage };
  }}), {}, { enabled: false, maxRetries: 0 }, {}, undefined,
), /empty summary/);
assert.equal(emptyCalls, 1, 'never advance or replay after an empty chunk summary');

const ascii = await summarize(`START_MARKER\n${'history text '.repeat(30000)}\nEND_MARKER`);
assert.ok(ascii.length > 1);
assert.ok(ascii.some((prompt) => prompt.includes('START_MARKER')));
assert.ok(ascii.some((prompt) => prompt.includes('END_MARKER')));

// The reported 272K-model session has about 500KB to summarize. It must
// not require the old sequence of at least 16 serial 32KB requests.
const largeWindow = await summarize('history '.repeat(62500), undefined, undefined, 272000);
assert.ok(largeWindow.length <= 4, `large-window summary took ${largeWindow.length} requests`);

// Near-ASCII UTF-8 must fill its byte budget; one multibyte character must
// not cut each request by 25%, or make a trailing newline an extra request.
const mixed = await summarize(('a'.repeat(1000) + 'é\n').repeat(1055), undefined, undefined, 272000);
assert.equal(mixed.length, 8);
await summarize('history '.repeat(62500), undefined, undefined, 128000, true);

const unicode = await summarize(`START_MARKER\n${'🙂漢字'.repeat(80000)}\nEND_MARKER`,
  'PREVIOUS_MARKER', undefined);
assert.ok(unicode.length > 1);
assert.ok(unicode.some((prompt) => prompt.includes('PREVIOUS_MARKER')));
assert.ok(unicode.some((prompt) => prompt.includes('END_MARKER')));

let attempted = false;
try {
  await summarize('large history '.repeat(30000), undefined, 'instruction '.repeat(10000));
  attempted = true;
} catch (error) {
  assert.match(error.message, /context budget/);
}
assert.equal(attempted, false);

const entries = [
  { type: 'message', id: 'u', parentId: null, timestamp: '2026-01-01T00:00:00.000Z',
    message: { role: 'user', content: `PREFIX ${'🙂漢字'.repeat(60000)}`, timestamp: 1 } },
  { type: 'message', id: 'a', parentId: 'u', timestamp: '2026-01-01T00:00:01.000Z',
    message: { role: 'assistant', content: [{ type: 'text', text: `kept ${'x'.repeat(120000)}` }],
      provider: 'openrouter', model: 'fake', stopReason: 'stop', timestamp: 2, usage } },
];
const preparation = prepareCompaction(entries, { reserveTokens: 16384, keepRecentTokens: 20000 });
assert.equal(preparation.isSplitTurn, true);
const splitRequests = [];
const splitProgress = [];
const splitStream = async (_model, context) => ({ result: async () => {
  const prompt = context.messages[0].content[0].text;
  splitRequests.push(prompt);
  assert.ok(Buffer.byteLength(prompt, 'utf8') <= (128000 - 16384) * 0.75,
    `oversized split-turn request: ${Buffer.byteLength(prompt, 'utf8')} bytes`);
  return { stopReason: 'stop', content: [{ type: 'text', text: 'split summary' }], usage };
} });
const split = await compact(preparation,
  { provider: 'openrouter', id: 'fake', contextWindow: 128000, maxTokens: 8192, reasoning: false },
  'local-fixture', {}, undefined, undefined, undefined, splitStream, {},
  { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } }, {
    onSummaryStart: (item) => splitProgress.push(item),
    onSummaryResponse: (_usage, item) => splitProgress.push(item),
  }, undefined);
assert.ok(splitRequests.length > 1);
assert.equal(split.usage.totalTokens, splitRequests.length * 2);
assert.equal(splitProgress[0].sourceBytesDone, 0);
assert.ok(splitProgress[0].sourceBytesTotal > 0);
assert.ok(splitProgress.every((item, index) => item.sourceBytesTotal === splitProgress[0].sourceBytesTotal &&
  item.sourceBytesDone >= (splitProgress[index - 1]?.sourceBytesDone ?? 0)));
assert.equal(splitProgress.at(-1).sourceBytesDone, splitProgress[0].sourceBytesTotal);
assert.equal(splitProgress.at(-1).summaryPhase, 'current-turn');
console.log(`bounded native compaction: ${ascii.length + unicode.length + splitRequests.length} local chunk requests`);
