import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';

const root = process.argv[2];
const {SessionManager, sessionEntryToContextMessages} = await import(
    pathToFileURL(`${root}/dist/core/session-manager.js`));
const {prepareCompaction, compact, DEFAULT_COMPACTION_SETTINGS} = await import(
    pathToFileURL(`${root}/dist/core/compaction/compaction.js`));
const model = {id:'fixture', api:'openai-completions', provider:'fixture',
    contextWindow:128000, maxTokens:4096, reasoning:false};
const usage = {input:10, output:5, cacheRead:0, cacheWrite:0, totalTokens:15,
    cost:{input:0, output:0, cacheRead:0, cacheWrite:0, total:0}};

for (const previous of [undefined, 'EARLIER_DECISION: preserve the original dataset']) {
    const manager = SessionManager.inMemory('/tmp');
    const first = manager.appendMessage({role:'user', content:'Continue the task', timestamp:1});
    if (previous !== undefined) manager.appendCompaction(previous, first, 100);
    for (let i = 0; i < 3; i++) manager.appendMessage({
        role:'assistant', content:[{type:'text', text:(`Progress ${i} `).repeat(90)}],
        api:model.api, provider:model.provider, model:model.id, stopReason:'stop', timestamp:i+2,
    });
    const preparation = prepareCompaction(manager.entryStore,
        {...DEFAULT_COMPACTION_SETTINGS, keepRecentTokens:100});
    assert.equal(preparation.isSplitTurn, true);
    assert.equal(preparation.messagesToSummarize.length, 0);
    assert.ok(preparation.turnPrefixMessages.length > 0);
    assert.equal(preparation.previousSummary, previous);

    let requests = 0;
    const streamFn = () => {
        requests++;
        const response = {role:'assistant', content:[{type:'text', text:'FRESH_PREFIX_SUMMARY'}],
            api:model.api, provider:model.provider, model:model.id,
            stopReason:'stop', timestamp:5, usage};
        const stream = (async function* () { yield {type:'done', reason:'stop', message:response}; })();
        stream.result = async () => response;
        return stream;
    };
    const result = await compact(preparation, model, 'unused', {}, undefined, undefined, 'off', streamFn);
    assert.equal(requests, 1, 'unchanged history needs no new provider request');
    const expectedHistory = previous ?? 'No prior history.';
    assert.ok(result.summary.startsWith(expectedHistory), result.summary);
    manager.appendCompaction(result.summary, result.firstKeptEntryId,
        result.tokensBefore, result.details);
    const context = JSON.stringify(manager.buildContextEntries()
        .flatMap(sessionEntryToContextMessages).toArray());
    assert.ok(context.includes(expectedHistory), 'next model context lost prior history');
    assert.ok(context.includes('FRESH_PREFIX_SUMMARY'), 'next context lost the new prefix');
    assert.ok(context.includes('Progress 2'), 'next context lost the retained suffix');
}
console.log('split compaction continuity: passed');
