// Local SDK/RPC host for selected-summary -> commit -> reopen -> original tests.
// Real pinned provider transport is restricted to the fixture loopback server.
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
const pkg = process.env.PI_COMPACTION_TEST_PACKAGE;
const root = process.env.PR95_OWNER_FIXTURE_ROOT;
if (!pkg || !root) throw Error('Owned package and fixture root required');
if (process.env.PR95_PRIVATE_SESSION === '1') process.umask(0o077);
const fetch = globalThis.fetch;
globalThis.fetch = (input, options) => {
  const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1') throw Error('Only fixture loopback allowed');
  return fetch(input, options);
};
const pi = await import(pathToFileURL(join(pkg, 'dist/index.js')));
const { runRpcMode } = await import(pathToFileURL(join(pkg, 'dist/modes/rpc/rpc-mode.js')));
const runtime = await pi.ModelRuntime.create({
  authPath: join(root, 'auth.json'), modelsPath: join(root, 'models.json'),
  modelsStorePath: join(root, 'models-store.json'),
});
const provider = process.env.PR95_CUSTOM_MODEL ? 'custom-local' : 'local-owner';
const modelId = process.env.PR95_CUSTOM_MODEL ? 'custom-model' : 'selected';
await runtime.setRuntimeApiKey(provider, 'offline-fixture');
const model = runtime.getModel(provider, modelId);
const settings = pi.SettingsManager.inMemory({
  compaction: { enabled: process.env.PR95_EFFECTIVE_DISABLED !== '1', reserveTokens: process.env.PR95_DECLINE_SUMMARY === "1" && process.env.PR95_COLD_DECLINE !== "1" ? 0 : 1000, keepRecentTokens: 10 },
  retry: { enabled: false },
});
const loader = new pi.DefaultResourceLoader({cwd: root, agentDir: root, settingsManager: settings});
await loader.reload();
const existing = process.argv.indexOf('--session');
const manager = existing >= 0 ? pi.SessionManager.open(process.argv[existing + 1]) :
  process.env.PR95_OWNER_SAVED_SESSION ? pi.SessionManager.open(process.env.PR95_OWNER_SAVED_SESSION) :
  pi.SessionManager.create(root, process.env.PR95_PRIVATE_SESSION === '1'
    ? join(root, 'native-sessions', 'f'.repeat(32)) : join(root, 'sessions'));
const { session } = await pi.createAgentSession({
  cwd: root, agentDir: root, modelRuntime: runtime, model, sessionManager: manager,
  settingsManager: settings, resourceLoader: loader, noTools: 'all',
});
const usage = {input: model.contextWindow - 500, output: 3, cacheRead: 0, cacheWrite: 0, totalTokens: model.contextWindow - 497,
  cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0}};
if (existing < 0 && !process.env.PR95_OWNER_SAVED_SESSION) {
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
if (process.env.PR95_DECLINE_SUMMARY === '1') {
  // A valid clean decline retains a context admitted under its original budget.
  // Change the effective reserve through the real settings owner after restore:
  // soft compaction is now requested, while the current native manager is ready.
  // The separate cold-decline case starts above its input budget and must refuse.
  settings.applyOverrides({compaction: {reserveTokens: 1000}});
  runtime.getAvailableSnapshot = () => [];
}
process.stderr.write(JSON.stringify({sessionFile: session.sessionFile, sessionId: session.sessionId,
  model: `${model.provider}/${model.id}`, contextWindow: model.contextWindow}) + '\n');
await runRpcMode({session, setRebindSession() {}, async dispose() { session.dispose(); }});
