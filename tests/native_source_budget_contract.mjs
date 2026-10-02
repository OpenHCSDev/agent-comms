/** Source sanity using original native estimators and the actual EntryStore.
 * This is not installed paused-turn or UI acceptance.
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {SourceTextModule, SyntheticModule} from 'node:vm';

const [packagePath, sourcePath] = process.argv.slice(2);
const api = join(packagePath, 'node_modules/@earendil-works/pi-ai/dist');
async function originalModule(path) {
    const exports = await import(pathToFileURL(path));
    return new SyntheticModule(Object.keys(exports), function () {
        for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    });
}
const budget = new SourceTextModule(readFileSync(join(sourcePath, 'stack/native-context-budget.mjs'), 'utf8'));
await budget.link(specifier => originalModule(resolve(api, 'api', specifier)));
await budget.evaluate();
const context = new SourceTextModule(readFileSync(join(sourcePath, 'stack/native-session-context.mjs'), 'utf8'));
await context.link(specifier => specifier.endsWith('agent-comms-context-budget.js')
    ? budget : originalModule(join(packagePath, 'dist/core/session-manager.js')));
await context.evaluate();
const {ContextBudget, BudgetAdmissionError} = budget.namespace;
const {SessionContext} = context.namespace;
const {SessionManager} = await import(pathToFileURL(join(packagePath, 'dist/core/session-manager.js')));
const model = {contextWindow: 272000, maxTokens: 128000};
const settings = {enabled: true, reserveTokens: 16384, keepRecentTokens: 20000};
const manager = SessionManager.inMemory(sourcePath);
manager.appendMessage({role: 'user', content: 'Original input, never replay', timestamp: 1});
manager.appendMessage({role: 'assistant', content: [{type: 'text', text: 'Reading'}],
    timestamp: 2, stopReason: 'toolUse', usage: {input: 254272, output: 0,
        cacheRead: 0, cacheWrite: 0, totalTokens: 254272}});
const session = {sessionManager: manager, model, systemPrompt: '',
    settingsManager: {getCompactionSettings: () => settings}, agent: {state: {tools: []}}};
SessionContext.restore(session).requireReady();
manager.appendMessage({role: 'toolResult', toolCallId: 'actual-read', toolName: 'read',
    content: [{type: 'text', text: 'x'.repeat(5537 * 4)}], timestamp: 3, isError: false});
const grown = SessionContext.sourceBudget(session);
assert.equal(grown.input, 259809);
assert(grown.compactionRequired(settings));
assert.throws(() => SessionContext.restore(session).requireReady());
// A cold source must include its system and tools, even with a tiny user message.
const cold = new ContextBudget({contextWindow: 8192, maxTokens: 1024}, {
    systemPrompt: 's'.repeat(32000), messages: [{role: 'user', content: 'test', timestamp: 1}],
    tools: [{name: 'read', description: 't'.repeat(1600), parameters: {type: 'object'}}],
});
assert(cold.compactionRequired({reserveTokens: 1024}));
assert.throws(() => cold.allowance(undefined), BudgetAdmissionError);
assert.equal(grown.allowance(undefined), undefined);
assert.equal(grown.allowance(128000), 12191);
// Final transformed overflow still refuses; sharing a source decision cannot waive it.
const final = new ContextBudget(model, {messages: []}, {
    messages: [{role: 'user', content: 'x'.repeat(272000 * 4), timestamp: 1}],
});
assert.throws(() => final.allowance(undefined), BudgetAdmissionError);
console.log('Source sanity passed: measured tool growth, cold system/tools, optional intent, final overflow. Installed pause/commit/continue/UI remains required.');
