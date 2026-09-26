import assert from 'node:assert/strict';
import { test } from 'node:test';
import { toPiResult } from '../src/tool-result.mjs';

test('MCP text and image results map to bounded Pi blocks with error attribution', () => {
  const png = Buffer.from('tiny-image').toString('base64');
  const result = toPiResult({ isError: true, content: [
    { type: 'text', text: 'server error' },
    { type: 'image', mimeType: 'image/png', data: png },
    { type: 'resource', resource: { uri: 'fixture://example', text: 'resource body' } },
  ], structuredContent: { answer: 42 } });
  assert.deepEqual(result.content.map((item) => item.type), ['text', 'text', 'image', 'text']);
  assert.equal(result.content[2].data, png);
  assert.match(result.content[0].text, /tool error/);
  assert.equal(result.details.mcpIsError, true);
  assert.equal(result.details.structuredContentPresent, true);
});

test('unsupported binary, malformed images and overlong text are explicit omissions', () => {
  const result = toPiResult({ content: [
    { type: 'audio', mimeType: 'audio/wav', data: 'secret-audio-base64' },
    { type: 'image', mimeType: 'image/png', data: '???secret' },
    { type: 'text', text: 'z'.repeat(50_000) },
  ] });
  assert.equal(result.details.truncated, true);
  assert.equal(JSON.stringify(result).includes('secret-audio-base64'), false);
  assert.ok(result.content.at(-1).text.includes('truncated'));
  assert.ok(result.content.every((item) => item.type === 'text'));
  assert.equal(toPiResult({ content: [] }).content[0].text, '(empty MCP result)');
});
