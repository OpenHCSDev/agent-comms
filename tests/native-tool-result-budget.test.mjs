import assert from 'node:assert/strict';
import { mkdtemp, readFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import {
    ContextExhaustingToolResult,
    ExternalizedToolResult,
    CorrectedToolResultCompaction,
    ToolResultArtifactStore,
    ToolResultCompactionState,
    budgetToolResult,
} from '../stack/native/native-tool-result-budget.mjs';

const call = { id: 'call/one', name: 'read' };
const settings = { enabled: true };

async function fixture(options = {}) {
    const root = await mkdtemp(join(tmpdir(), 'pi-tool-result-budget-'));
    const content = options.content ?? [{ type: 'text', text: 'small result' }];
    const result = await budgetToolResult({
        toolCall: call,
        content,
        details: options.details,
        contextTokens: options.contextTokens ?? 1000,
        contextWindow: options.contextWindow ?? 49152,
        reserveTokens: options.reserveTokens ?? 16384,
        parallelCalls: options.parallelCalls ?? 1,
        artifactStore: options.artifactStore ?? new ToolResultArtifactStore('session-one', root),
    });
    return { content, result, root };
}

test('a result inside its live share is unchanged and its conservative correction stays below threshold', async () => {
    const { content, result } = await fixture();
    assert.equal(result.content, content);
    const state = result.nextCompactionState(ToolResultCompactionState.shared);
    assert.ok(state instanceof CorrectedToolResultCompaction);
    assert.equal(state.shouldCompact(1000, 49_152, settings, (tokens, window) =>
        tokens > window - 16_384), false);
});

test('an unknown context window leaves the result unchanged instead of inventing a zero budget', async () => {
    const content = [{ type: 'text', text: 'x'.repeat(100_000) }];
    const { result } = await fixture({ content, contextWindow: 0 });
    assert.equal(result.content, content);
});

test('a large result is externalized with a bounded head and tail plus exact omitted range', async () => {
    const text = `${'h'.repeat(20_000)}${'m'.repeat(20_000)}${'t'.repeat(20_000)}`;
    const { result } = await fixture({ content: [{ type: 'text', text }], contextWindow: 128_000 });
    assert.ok(result instanceof ExternalizedToolResult);
    assert.ok(!(result instanceof ContextExhaustingToolResult));
    assert.ok(result.content[0].text.length < text.length);
    assert.match(result.content[0].text, /^h+/);
    assert.match(result.content[0].text, /t+\n\n\[Tool result externalized:/);
    assert.match(result.content[0].text, /omitted UTF-16 indices \d+-\d+ of 60000/);
    assert.match(result.content[0].text, /Complete result: /);
    const stored = JSON.parse(await readFile(result.artifact.path, 'utf8'));
    assert.deepEqual(stored.content, [{ type: 'text', text }]);
    assert.equal((await stat(result.artifact.path)).mode & 0o777, 0o600);
    const state = result.nextCompactionState(ToolResultCompactionState.shared);
    assert.ok(state instanceof CorrectedToolResultCompaction);
    assert.equal(state.shouldCompact(1000, 128_000, settings, (tokens, window) =>
        tokens > window - 16_384), false);
});

test('the bounded persisted result corrects Pi estimation before deciding compaction', async () => {
    const text = 'é'.repeat(18_000);
    const { result } = await fixture({
        content: [{ type: 'text', text }],
        contextTokens: 32_700,
    });
    assert.ok(result instanceof ContextExhaustingToolResult);
    const state = result.nextCompactionState(ToolResultCompactionState.shared);
    assert.ok(state instanceof CorrectedToolResultCompaction);
    assert.ok(state.correctionTokens > 0);
    assert.equal(state.shouldCompact(32_700, 49_152, settings, (tokens, window, value) =>
        value.enabled && tokens > window - 16_384), true);
    assert.equal(state.shouldCompact(32_700, 49_152, { enabled: false }, (tokens, window, value) =>
        value.enabled && tokens > window - 16_384), false);
});

test('parallel tool calls divide the result share before either result is committed', async () => {
    const content = [{ type: 'text', text: 'p'.repeat(6_000) }];
    const single = await fixture({ content, parallelCalls: 1 });
    const parallel = await fixture({ content, parallelCalls: 4 });
    assert.equal(single.result.content, content);
    assert.ok(parallel.result instanceof ExternalizedToolResult);
});

test('artifact failure returns a bounded rejection with its omitted range and reason', async () => {
    const text = 'q'.repeat(80_000);
    const artifactStore = { persist: async () => { throw new Error('disk read-only'); } };
    const { result } = await fixture({ content: [{ type: 'text', text }], artifactStore });
    assert.ok(result instanceof ContextExhaustingToolResult);
    assert.ok(result.content[0].text.length < text.length);
    assert.match(result.content[0].text, /Omitted range: text block 0: omitted UTF-16 indices/);
    assert.match(result.content[0].text, /Reason: disk read-only/);
});

test('a non-JSON details value is called out without losing complete content', async () => {
    const text = 'd'.repeat(60_000);
    const details = {};
    details.self = details;
    const { result } = await fixture({ content: [{ type: 'text', text }], details });
    const stored = JSON.parse(await readFile(result.artifact.path, 'utf8'));
    assert.equal(stored.detailsOmission.reason, 'tool result details were not JSON-serializable');
    assert.match(result.content[0].text, /Artifact fallback: details omitted/);
});
