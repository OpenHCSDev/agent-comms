// Test-only SDK/RPC host for inline reactions excluded by the immutable CLI.
// Uses the caller's verified bundle; never patches or copies that package.
import { existsSync, appendFileSync } from 'node:fs';
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
// Emit through the actual pinned extension UI implementation and RPC transport.
if (process.env.S1_UI_PROBE) {
  extensions.push((api) => {
    api.on('message_end', async (event, ctx) => {
      if (event.message.role !== 'user') return;
      const receipt = {version: 1, source: 'pi-mcp-client', inputId: event.message.inputId,
        state: 'running', lifetime: 'turn', servers: []};
      ctx.ui.setStatus('pi-mcp/live-v1', JSON.stringify({...receipt, inputId: '0'.repeat(32)}));
      ctx.ui.setStatus('pi-mcp/live-v1', JSON.stringify(receipt));
      ctx.ui.setStatus('pi-mcp/live-v1', JSON.stringify(receipt));
      const confirmed = await ctx.ui.confirm('Confirm native operation', 'This turn only');
      const selected = await ctx.ui.select('Choose native option', ['one', 'two']);
      const input = await ctx.ui.input('Unsupported input');
      const edited = await ctx.ui.editor('Unsupported editor', 'original');
      appendFileSync(process.env.S1_UI_PROBE, JSON.stringify({inputId: event.message.inputId,
        confirmed, selected: selected ?? null, input: input ?? null, edited: edited ?? null}) + '\n');
    });
  });
}
if (process.env.S1_REPLACEMENT_PROBE) {
  extensions.push((api) => {
    api.on('session_start', (event, ctx) => appendFileSync(process.env.S1_REPLACEMENT_PROBE,
      JSON.stringify({event: 'session_start', reason: event.reason,
        sessionId: ctx.sessionManager.getSessionId()}) + '\n'));
    api.on('session_before_switch', (event) => {
      if (event.targetSessionFile === process.env.S1_CANCELLED_SESSION) return {cancel: true};
    });
  });
}
if (process.env.S1_COMPACTION_PROBE) {
  extensions.push((api) => {
    let original;
    const record = (event, ctx) => appendFileSync(process.env.S1_COMPACTION_PROBE,
      JSON.stringify({event: event.type, sessionId: ctx.sessionManager.getSessionId(),
        originalSessionId: original?.sessionManager.getSessionId(),
        entryId: event.compactionEntry?.id, reason: event.reason}) + '\n');
    api.on('session_start', (event, ctx) => { original = ctx; record(event, ctx); });
    api.on('session_shutdown', record);
    api.on('session_compact', record);
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
const manager = process.argv.includes('--session') ? pi.SessionManager.open(option('--session')) :
  pi.SessionManager.create(cwd, join(agentDir, 'sessions'));
const host = await pi.createAgentSessionRuntime(async (options) => {
  const services = await pi.createAgentSessionServices({...options, modelRuntime: runtime,
    settingsManager: settings, resourceLoaderOptions: {
      noExtensions: true, noSkills: true, noContextFiles: true, noPromptTemplates: true,
      extensionFactories: extensions,
    }});
  const result = await pi.createAgentSessionFromServices({services,
    sessionManager: options.sessionManager, sessionStartEvent: options.sessionStartEvent,
    model: runtime.getModel(option('--provider'), option('--model')), thinkingLevel: 'off',
    ...(process.env.S1_COMMS_TOOLS ? {tools: ['read', ...customTools.map(tool => tool.name)], customTools} : {noTools: 'all'}),
  });
  return {...result, services, diagnostics: services.diagnostics};
}, {
  cwd, agentDir, sessionManager: manager,
});
await runRpcMode(host);
