/** Source policy control using Pi's original summary envelope, no provider. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const { createCompactionSummaryMessage } = await import(pathToFileURL(
    process.env.AGENT_COMMS_PI_PACKAGE_DIR + '/dist/core/messages.js'));
const { CompactionPolicy } = await import(pathToFileURL(
    process.env.AGENT_COMMS_PI_PACKAGE_DIR + '/dist/core/compaction/agent-comms-policy.js'));
const exact = readFileSync(process.argv[2], 'utf8');
const policy = new CompactionPolicy();
const model = { contextWindow: 12000, maxTokens: 4096 };
const recent = Object.freeze([
    Object.freeze({ role: 'assistant', content: [{ type: 'toolCall', id: 'original-tool', name: 'read', arguments: { path: '/artifacts/original' } }] }),
    Object.freeze({ role: 'toolResult', toolCallId: 'original-tool', content: [{ type: 'text', text: 'Original result' }] }),
]);
const original = JSON.stringify(recent);
let prior = '';
for (let round = 0; round < 3; round++) {
    const packed = policy.packSummary(exact, prior + 'narrative 🌿 '.repeat(5000),
        '\nFiles: /artifacts/original', 1000, recent, model, 2048,
        createCompactionSummaryMessage);
    assert.ok(packed.startsWith(exact + '\n\n'));
    assert.ok(packed.endsWith('\nFiles: /artifacts/original'));
    assert.ok(packed.isWellFormed());
    policy.requireContext([createCompactionSummaryMessage(packed, 1000, 1), ...recent], model, 2048);
    assert.equal(JSON.stringify(recent), original);
    prior = packed;
}
assert.throws(() => policy.packSummary(exact.repeat(100), 'Optional narrative', '',
    1000, recent, model, 2048, createCompactionSummaryMessage), /Mandatory exact/);
console.log('PASS: three policy packing rounds, exact source retained, native envelope fits, tool pair unchanged, mandatory overflow refused. Not native checkpoint or recall acceptance.');
