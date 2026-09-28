// Local SDK/RPC host for selected-summary -> commit -> reopen -> original tests.
// Every model stream is synthetic and outbound fetch is forbidden.
import { join } from 'node:path';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
const pkg = process.env.PI_COMPACTION_TEST_PACKAGE;
const root = process.env.PR95_OWNER_FIXTURE_ROOT;
if (!pkg || !root) throw Error('Owned package and fixture root required');
if (process.env.PR95_PRIVATE_SESSION === '1') process.umask(0o077);
globalThis.fetch = () => { throw Error('NETWORK PROHIBITED'); };
const pi = await import(pathToFileURL(join(pkg, 'dist/index.js')));
const { runRpcMode } = await import(pathToFileURL(join(pkg, 'dist/modes/rpc/rpc-mode.js')));
const { createAssistantMessageEventStream } = await import(pathToFileURL(
  join(pkg, 'node_modules/@earendil-works/pi-ai/dist/utils/event-stream.js')));
const runtime = await pi.ModelRuntime.create({
  authPath: join(root, 'auth.json'), modelsPath: join(root, 'models.json'),
  modelsStorePath: join(root, 'models-store.json'),
});
const provider = process.env.PR95_CUSTOM_MODEL ? 'custom-local' : 'openai';
const modelId = process.env.PR95_CUSTOM_MODEL ? 'custom-model' : 'gpt-4.1-mini';
await runtime.setRuntimeApiKey(provider, 'offline-fixture');
const model = runtime.getModel(provider, modelId);
const settings = pi.SettingsManager.inMemory({
  compaction: { enabled: process.env.PR95_EFFECTIVE_DISABLED !== '1', reserveTokens: 1000, keepRecentTokens: 10 },
  retry: { enabled: false },
});
const loader = new pi.DefaultResourceLoader({cwd: root, agentDir: root, settingsManager: settings});
await loader.reload();
const existing = process.argv.indexOf('--session');
const manager = existing >= 0 ? pi.SessionManager.open(process.argv[existing + 1]) :
  pi.SessionManager.create(root, process.env.PR95_PRIVATE_SESSION === '1'
    ? join(root, 'native-sessions', 'f'.repeat(32)) : join(root, 'sessions'));
const { session } = await pi.createAgentSession({
  cwd: root, agentDir: root, modelRuntime: runtime, model, sessionManager: manager,
  settingsManager: settings, resourceLoader: loader, noTools: 'all',
});
const usage = {input: 12, output: 3, cacheRead: 0, cacheWrite: 0, totalTokens: 15,
  cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0}};
if (existing < 0) {
  for (let i = 0; i < 5; i++) {
    const text = 'Question ' + i;
    const tracked = process.env.PR95_PRIVATE_SESSION === '1' ? {
      inputId: String(i + 1).padStart(32, '0'),
      inputDigest: createHash('sha256').update('pi-input-request-v1\n' + JSON.stringify({
        kind: 'prompt', text, images: null, streamingBehavior: null,
        expandPromptTemplates: true, source: 'rpc'})).digest('hex'),
    } : {};
    manager.appendMessage({role: 'user', content: [{type: 'text', text}], ...tracked, timestamp: 2 * i});
    manager.appendMessage({role: 'assistant', content: [{type: 'text', text: 'Answer ' + i}],
      provider: model.provider, model: model.id, api: model.api, usage,
      stopReason: 'stop', timestamp: 2 * i + 1});
  }
  session.agent.state.messages = manager.buildContextEntries().flatMap(pi.sessionEntryToContextMessages).toArray();
}
session.agent.streamFunction = (_model, context) => {
  const previous = manager.entryStore.latest(manager.getLeafId(), 'compaction');
  if (previous) {
    // Verify the actual native provider context retains the committed summary,
    // including the second compaction's previous-summary and reopened original.
    assert.ok(JSON.stringify(context.messages).includes(previous.summary));
  }
  const events = createAssistantMessageEventStream();
  const message = {role: 'assistant', content: [{type: 'text', text: 'Synthetic summary or answer'}],
    api: model.api, provider: model.provider, model: model.id, usage,
    stopReason: 'stop', timestamp: Date.now()};
  queueMicrotask(() => {
    events.push({type: 'start', partial: {...message, content: []}});
    events.push({type: 'text_delta', contentIndex: 0, delta: message.content[0].text, partial: message});
    events.push({type: 'done', reason: 'stop', message});
    events.end(message);
  });
  return events;
};
if (process.env.PR95_DECLINE_SUMMARY === '1') runtime.getAvailableSnapshot = () => [];
process.stderr.write(JSON.stringify({sessionFile: session.sessionFile, sessionId: session.sessionId,
  model: `${model.provider}/${model.id}`, contextWindow: model.contextWindow}) + '\n');
await runRpcMode({session, setRebindSession() {}, async dispose() { session.dispose(); }});
