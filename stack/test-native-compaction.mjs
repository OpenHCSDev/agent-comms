// Credential-free proof that a long history cannot become one oversized summary request.
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const path = resolve(import.meta.dirname, '.pi-native/node_modules/@earendil-works/pi-coding-agent/dist/core/compaction/compaction.js');
const { generateSummaryWithUsage } = await import(pathToFileURL(path).href);
const usage = {
  input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};
const requests = [];
const stream = async (_model, context) => ({
  result: async () => {
    const prompt = context.messages[0].content[0].text;
    requests.push(prompt);
    assert.ok(prompt.length < 70000, `oversized compaction request: ${prompt.length}`);
    return { stopReason: 'stop', content: [{ type: 'text', text: `summary ${requests.length}` }], usage };
  },
});
const body = `START_MARKER\n${'history text '.repeat(30000)}\nEND_MARKER`;
const result = await generateSummaryWithUsage(
  [{ role: 'user', content: [{ type: 'text', text: body }], timestamp: Date.now() }],
  { provider: 'openrouter', id: 'fake', contextWindow: 128000, maxTokens: 8192, reasoning: false },
  16384, 'local-fixture', {}, undefined, undefined, undefined, undefined,
  stream, {}, { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } }, {}, undefined,
);
assert.ok(requests.length > 1);
assert.ok(requests.some((prompt) => prompt.includes('START_MARKER')));
assert.ok(requests.some((prompt) => prompt.includes('END_MARKER')));
assert.equal(result.usage.totalTokens, requests.length * 2);
console.log(`bounded native compaction: ${requests.length} local requests`);
