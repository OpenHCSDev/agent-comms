// Offline inspection uses the actual native preparation and policy owners.
// Only aggregate sizes/identities leave this script; never print source content.
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
import { statSync } from 'node:fs';
const [packagePath, savedCopy] = process.argv.slice(2);
globalThis.fetch = () => { throw new Error('Offline inspection prohibits provider calls'); };
const { SessionManager } = await import(pathToFileURL(join(packagePath, 'dist/core/session-manager.js')));
const { prepareCompaction } = await import(pathToFileURL(join(packagePath, 'dist/core/compaction/compaction.js')));
const { HistorySummarySource } = await import(pathToFileURL(join(packagePath, 'dist/core/compaction/agent-comms-source.js')));
const { CompactionPolicy } = await import(pathToFileURL(join(packagePath, 'dist/core/compaction/agent-comms-policy.js')));
const model = {provider:'openai-codex', id:'gpt-6.1-sol', contextWindow:272000, maxTokens:128000};
const settings = {reserveTokens:16384, keepRecentTokens:20000};
const started = performance.now();
const manager = SessionManager.open(savedCopy);
const preparation = prepareCompaction(manager.entryStore, settings, model, manager.getLeafId());
const policy = new CompactionPolicy();
const chunkTokens = policy.sourceTokens(model, settings.reserveTokens);
const sources = [
    ['history', new HistorySummarySource(preparation.messagesToSummarize, preparation.previousSummary)],
    ['current_turn', new HistorySummarySource(preparation.turnPrefixMessages)],
];
const selected = sources.map(([phase, source]) => {
    let bytes = 0, chunks = 0;
    for (const chunk of source.chunks(chunkTokens)) { bytes += Buffer.byteLength(chunk); chunks++; }
    return {phase, bytes, planned_map_segments:chunks};
});
console.log(JSON.stringify({
    raw_saved_bytes:statSync(savedCopy).size, context_window:model.contextWindow,
    estimated_context_tokens:preparation.tokensBefore, previous_compaction:!!preparation.previousSummary,
    is_split_turn:preparation.isSplitTurn, first_kept_entry_id:preparation.firstKeptEntryId,
    selected_sources:selected, input_tokens:policy.inputTokens(model,settings.reserveTokens),
    chunk_tokens:chunkTokens, workers:policy.concurrency,
    planned_summary_output_tokens:policy.summaryTokens(model,policy.inputTokens(model,settings.reserveTokens),settings.reserveTokens),
    offline_plan_seconds:(performance.now()-started)/1000,
},null,2));
