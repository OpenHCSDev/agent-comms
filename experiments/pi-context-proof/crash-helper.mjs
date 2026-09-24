// Disposable child: SIGKILL after Pi's native fsynced context-ready journal,
// before any provider call or assistant session entry. No network/API use.
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const pkg = process.env.PI_PACKAGE_DIR;
const root = process.env.PI_CRASH_ROOT;
if (!pkg?.startsWith('/var/tmp/') || !root?.startsWith('/var/tmp/')) process.exit(90);
const pi = await import(pathToFileURL(join(pkg,'dist/index.js')).href);
const cwd = join(root,'work');
const agentDir = join(root,'agent');
const runtime = await pi.ModelRuntime.create({
  authPath:join(root,'auth.json'),modelsPath:join(root,'models.json'),
  modelsStorePath:join(root,'models-store.json'),
});
await runtime.setRuntimeApiKey('openai','fake-local-only');
const settings=pi.SettingsManager.inMemory({compaction:{enabled:false},retry:{enabled:false}});
const loader=new pi.DefaultResourceLoader({cwd,agentDir,settingsManager:settings});
await loader.reload();
const sm=pi.SessionManager.create(cwd,join(root,'sessions'));
const {session}=await pi.createAgentSession({cwd,agentDir,modelRuntime:runtime,
  model:runtime.getModel('openai','gpt-4.1-mini'),sessionManager:sm,
  settingsManager:settings,resourceLoader:loader,noTools:'all'});
const original=session.agent.onContextReady;
session.agent.onContextReady=async context => {
  await original(context);
  process.kill(process.pid,'SIGKILL');
};
session.agent.streamFunction=async () => {throw Error('Provider must not run before crash');};
await session.prompt('crash after context assembly', {inputId:'f'.repeat(32)});
process.exit(91);
