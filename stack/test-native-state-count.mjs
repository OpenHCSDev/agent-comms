// End validation for the shared selected-entry count. No native input/provider.
import assert from 'node:assert/strict';
import {appendFileSync, mkdirSync, writeFileSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const [packagePath, sourcePath, directory] = process.argv.slice(2);
const pkg = resolve(packagePath);
const root = resolve(directory);
mkdirSync(root, {recursive: true, mode: 0o700});
const {MemoryEntryStore, DiskEntryStore} = await import(pathToFileURL(join(pkg, 'dist/core/session-entry-store.js')));
const {SessionManager, sessionEntryToContextMessages} = await import(pathToFileURL(join(pkg, 'dist/core/session-manager.js')));
const {CompactionContext, SessionContext} = await import(pathToFileURL(join(pkg, 'dist/core/session-context.js')));
const header = {type:'session', version:3, id:'state-count', cwd:root, timestamp:'2026-10-02T00:00:00Z'};
const entries = [
    {type:'message', id:'a', parentId:null, message:{role:'user', content:'first'}},
    {type:'custom_message', id:'b', parentId:'a', customType:'fixture', content:'custom', display:true},
    {type:'branch_summary', id:'c', parentId:'b', summary:''},
    {type:'model_change', id:'d', parentId:'c', provider:'fixture', modelId:'not-dispatched'},
    {type:'branch_summary', id:'e', parentId:'d', summary:'branch'},
    {type:'compaction', id:'f', parentId:'e', summary:'summary', firstKeptEntryId:'b', tokensBefore:100},
    {type:'message', id:'g', parentId:'f', message:{role:'assistant', content:[]}},
    {type:'message', id:'h', parentId:'a', message:{role:'user', content:'other branch'}},
];
const file = join(root, 'session.jsonl');
writeFileSync(file, [header,...entries].map(row => JSON.stringify(row)).join('\n')+'\n', {mode:0o600});
const stores = [new MemoryEntryStore(header, entries), new DiskEntryStore(file, {indexDirectory:root})];
try {
    for (const store of stores) {
        assert.equal(store.contextMessageCount('g'), 4, 'compacted kept suffix excludes earlier source');
        assert.equal(store.contextMessageCount('h'), 2, 'other branch excludes the compaction');
        assert.equal(store.contextMessageCount(null), 0, 'empty branch');
        assert.deepEqual([...store.contextEntries('g')].map(entry => entry.id), ['f','b','c','d','e','g']);
        const get = store.get;
        store.get = () => {throw new Error('Count decoded an entry body');};
        try {assert.equal(store.contextMessageCount('g'), 4);} finally {store.get = get;}
    }
    appendFileSync(file, '\n');
    assert.throws(() => stores[1].contextMessageCount('g'), /revision changed/, 'stale selectors never answer');
} finally {for (const store of stores) store.close();}

// Original retained source uses the same installed body projection and selectors.
const source = SessionManager.open(resolve(sourcePath));
try {
    const originalMessages = source.buildContextEntries().flatMap(sessionEntryToContextMessages).toArray();
    const expected = originalMessages.length;
    const context = new CompactionContext(source);
    const get = source.entryStore.get;
    source.entryStore.get = () => {throw new Error('Compaction count decoded a source body');};
    try {assert.equal(context.messageCount(), expected);} finally {source.entryStore.get = get;}

    // Restore must budget and install the same original acquisition. An oversized
    // context still belongs to CompactionContext, with no resident history.
    const selectedEntries = [...source.entryStore.contextMetadata(source.getLeafId())].length;
    const session = {sessionManager:source, systemPrompt:'', model:{contextWindow:1e9},
        settingsManager:{getCompactionSettings:()=>({reserveTokens:1})},
        agent:{state:{messages:[], tools:[]}}};
    let bodyReads=0;
    source.entryStore.get = function(id) {bodyReads++; return get.call(this, id);};
    try {
        SessionContext.restore(session).requireReady();
        assert.deepEqual(session.agent.state.messages, originalMessages);
        assert.equal(bodyReads, selectedEntries, 'ready restore decoded the selected prefix twice');
        bodyReads=0;
        session.model={contextWindow:1};
        SessionContext.restore(session);
        assert.throws(()=>session.storedContext.requireReady(), /did not admit/);
        assert.deepEqual(session.agent.state.messages, []);
        assert.equal(bodyReads, selectedEntries, 'compaction selection repeats history acquisition');
    } finally {source.entryStore.get = get;}
    console.log(JSON.stringify({ok:true, retained_message_count:expected,
        provider_inputs:0, body_reads_for_count:0, branches_and_revision_refusal:true,
        ready_restore_body_reads:selectedEntries, over_budget_history_uninstalled:true}));
} finally {source.entryStore.close();}
