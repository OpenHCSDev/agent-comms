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
    if (previous !== undefined) manager.appendCompaction(previous, first, 100, {
        readFiles:Array.from({length:5000}, (_,i)=>`/old/session/file-${i}.py`), modifiedFiles:['/old/edited.py'],
    });
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
    const prompts = [];
    const streamFn = (_model, context) => {
        requests++;
        const prompt = context.messages[0].content[0].text;
        prompts.push(prompt);
        const text = prompt.includes("<previous-summary>") ? previous : "FRESH_PREFIX_SUMMARY";
        const response = {role:'assistant', content:[{type:'text', text}],
            api:model.api, provider:model.provider, model:model.id,
            stopReason:'stop', timestamp:5, usage};
        const stream = (async function* () { yield {type:'done', reason:'stop', message:response}; })();
        stream.result = async () => response;
        return stream;
    };
    const result = await compact(preparation, model, 'unused', {}, undefined, undefined, 'off', streamFn);
    assert.equal(requests, previous ? 2 : 1, 'every prior summary is reduced instead of copied forever');
    assert.deepEqual(result.details, {readFiles:[], modifiedFiles:[]}, 'old file history leaked into the new interval');
    assert.ok(prompts.every(p=>!p.includes('/old/session/file-')), 'lifetime metadata reached summarization');
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
// Real branch entries retain the recent file operation, not the old summary inventory.
const {prepareBranchEntries, collectEntriesForBranchSummary, generateBranchSummary} = await import(
    pathToFileURL(`${root}/dist/core/compaction/branch-summarization.js`));
const branchManager = SessionManager.inMemory('/tmp');
const base = branchManager.appendMessage({role:'user',content:'Current branch work',timestamp:1});
branchManager.branchWithSummary(base, 'Finished exploration', {readFiles:['/old/exploration.py'],modifiedFiles:[]});
branchManager.appendMessage({role:'assistant',content:[{type:'toolCall',id:'read-current',name:'read',arguments:{path:'/current/task.py'}}],
    api:model.api,provider:model.provider,model:model.id,stopReason:'toolUse',timestamp:2});
const range = collectEntriesForBranchSummary(branchManager, branchManager.getLeafId(), base).entries;
const prepared = prepareBranchEntries(range,1000);
assert.deepEqual([...prepared.fileOps.read],['/current/task.py']);
const result = await generateBranchSummary(range, {model,apiKey:'unused',streamFn:()=>{
    const response={role:'assistant',content:[{type:'text',text:'Continue /current/task.py'}],api:model.api,
        provider:model.provider,model:model.id,stopReason:'stop',timestamp:5,usage};
    const stream=(async function*(){yield {type:'done',reason:'stop',message:response};})();
    stream.result=async()=>response;return stream;
}});
assert.deepEqual(result.readFiles,['/current/task.py']);
assert.equal(result.summary,'The user explored a different conversation branch before returning here.\nSummary of that exploration:\n\nContinue /current/task.py');
console.log('compaction current-work and branch interval: passed');
