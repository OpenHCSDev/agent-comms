// Credential-free proof that a long history cannot become one oversized summary request.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const manifest = readFileSync(resolve(import.meta.dirname, 'pi-native.sha256'));
const buildId = createHash('sha256').update(manifest).digest('hex').slice(0, 16);
const path = resolve(import.meta.dirname, `.pi-native-${buildId}/node_modules/@earendil-works/pi-coding-agent/dist/core/compaction/compaction.js`);
const { generateSummaryWithUsage } = await import(pathToFileURL(path).href);
const usage = {
  input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};
async function summarize(body, previousSummary, instructions) {
  const requests = [];
  const stream = async (_model, context) => ({
    result: async () => {
      const prompt = context.messages[0].content[0].text;
      requests.push(prompt);
      assert.ok(Buffer.byteLength(prompt, 'utf8') < 64000,
        `oversized compaction request: ${Buffer.byteLength(prompt, 'utf8')} bytes`);
      return { stopReason: 'stop', content: [{ type: 'text', text: `summary ${requests.length}` }], usage };
    },
  });
  const result = await generateSummaryWithUsage(
    [{ role: 'user', content: [{ type: 'text', text: body }], timestamp: Date.now() }],
    { provider: 'openrouter', id: 'fake', contextWindow: 128000, maxTokens: 8192, reasoning: false },
    16384, 'local-fixture', {}, undefined, instructions, previousSummary, undefined,
    stream, {}, { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } }, {}, undefined,
  );
  assert.equal(result.usage.totalTokens, requests.length * 2);
  return requests;
}

const small = await summarize('short history', undefined, undefined);
assert.equal(small.length, 1);

const ascii = await summarize(`START_MARKER\n${'history text '.repeat(30000)}\nEND_MARKER`);
assert.ok(ascii.length > 1);
assert.ok(ascii.some((prompt) => prompt.includes('START_MARKER')));
assert.ok(ascii.some((prompt) => prompt.includes('END_MARKER')));

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
console.log(`bounded native compaction: ${ascii.length + unicode.length} local chunk requests`);
