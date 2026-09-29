// Test-only SDK/RPC host for inline reactions excluded by the immutable CLI.
// Uses the caller's verified bundle; never patches or copies that package.
import { existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const pkg = process.env.S1_NATIVE_PACKAGE;
const agentDir = process.env.PI_CODING_AGENT_DIR;
const allowed = process.env.S1_LOCAL_ORIGIN;
if (!pkg || !agentDir || !allowed?.startsWith('http://127.0.0.1:')) throw Error('Isolated host required');
const fetch = globalThis.fetch;
globalThis.fetch = (url, ...args) => {
  const target = url instanceof Request ? url.url : String(url);
  if (new URL(target).origin !== allowed) throw Error('BLOCKED_NONLOCAL_NETWORK');
  return fetch(url, ...args);
};
const pi = await import(pathToFileURL(join(pkg, 'dist/index.js')));
const { runRpcMode } = await import(pathToFileURL(join(pkg, 'dist/modes/rpc/rpc-mode.js')));
const option = (name) => process.argv[process.argv.indexOf(name) + 1];
const cwd = process.cwd();
const runtime = await pi.ModelRuntime.create({
  authPath: join(agentDir, 'auth.json'), modelsPath: join(agentDir, 'models.json'),
  modelsStorePath: join(agentDir, 'models-store.json'),
});
await runtime.setRuntimeApiKey(option('--provider'), 'localhost-only');
const settings = pi.SettingsManager.inMemory(process.env.S1_NATIVE_SETTINGS ?
  JSON.parse(process.env.S1_NATIVE_SETTINGS) : {compaction: {enabled: false}, retry: {enabled: false}});
const extensions = [];
if (process.env.S1_DELAY_SETTLEMENT) {
  extensions.push((api) => {
    let count = 0;
    api.on('agent_settled', async () => {
      if (++count === 1) {
        while (!existsSync(process.env.S1_DELAY_SETTLEMENT)) {
          await new Promise(resolve => setTimeout(resolve, 10));
        }
      }
    });
  });
}
// Tool declarations and output bounding remain owned by the Python CLI. The
// fixture supplies only the SDK transport binding, not another tool catalog.
const invoke = (args) => JSON.parse(execFileSync(process.env.S1_PYTHON,
  ['-m', 'agent_comms.cli', ...args], {encoding: 'utf8', timeout: 30000}));
const customTools = process.env.S1_COMMS_TOOLS ? invoke(['tools']).tools.map(declaration => ({
  ...declaration,
  async execute(_id, params) {
    const result = invoke(['invoke', '--tool', declaration.name, '--arguments', JSON.stringify(params)]);
    return {content: [{type: 'text', text: JSON.stringify(result, null, 2)}], details: result};
  },
})) : [];
const loader = new pi.DefaultResourceLoader({cwd, agentDir, settingsManager: settings,
  noExtensions: true, noSkills: true, noContextFiles: true, noPromptTemplates: true,
  extensionFactories: extensions});
await loader.reload();
const manager = process.argv.includes('--session') ? pi.SessionManager.open(option('--session')) :
  pi.SessionManager.create(cwd, join(agentDir, 'sessions'));
const {session} = await pi.createAgentSession({cwd, agentDir, modelRuntime: runtime,
  model: runtime.getModel(option('--provider'), option('--model')), thinkingLevel: 'off',
  settingsManager: settings, sessionManager: manager, resourceLoader: loader,
  ...(process.env.S1_COMMS_TOOLS ? {tools: ['read', ...customTools.map(tool => tool.name)], customTools} : {noTools: 'all'}),
});
await runRpcMode({session, setRebindSession() {}, async dispose() {session.dispose();}});
