// Offline integration of the production services factory and retry isolation.
import assert from 'node:assert/strict';
import {mkdtempSync, mkdirSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const pkg = process.env.PI_COMPACTION_TEST_PACKAGE;
assert.ok(pkg);
const root = mkdtempSync(join(tmpdir(), 'pr95-settings-'));
globalThis.fetch = () => { throw Error('NETWORK PROHIBITED'); };
try {
  const canonical = join(root, 'canonical'), isolated = join(root, 'isolated');
  mkdirSync(canonical); mkdirSync(isolated); mkdirSync(join(root, '.pi'));
  writeFileSync(join(canonical, 'settings.json'), JSON.stringify({
    compaction: {enabled:true, reserveTokens:321, keepRecentTokens:123},
    retry: {enabled:true, maxRetries:99, provider:{maxRetries:99}},
  }));
  writeFileSync(join(isolated, 'settings.json'), JSON.stringify({compaction:{enabled:false}}));
  writeFileSync(join(root, '.pi/settings.json'), JSON.stringify({compaction:{keepRecentTokens:456}}));
  process.env.AGENT_COMMS_NATIVE_CONFIG_DIR = canonical;
  process.env.PI_CODING_AGENT_DIR = isolated;
  const pi = await import(pathToFileURL(join(pkg, 'dist/index.js')));
  const runtime = await pi.ModelRuntime.create({authPath:join(canonical, 'auth.json'),
    modelsPath:join(canonical, 'models.json'), modelsStorePath:join(root, 'models-store.json')});
  const services = await pi.createAgentSessionServices({cwd:root, agentDir:isolated,
    modelRuntime:runtime, resourceLoaderOptions:{noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true}});
  assert.deepEqual(services.settingsManager.getCompactionSettings(),
    {enabled:true, reserveTokens:321, keepRecentTokens:456});
  assert.equal(services.settingsManager.getRetryEnabled(), false);
  const retry = services.settingsManager.getRetrySettings();
  assert.equal(retry.maxRetries, 0);
  services.settingsManager.setProjectTrusted(false);
  assert.equal(services.settingsManager.getCompactionSettings().keepRecentTokens, 123);
  assert.equal(services.settingsManager.getRetryEnabled(), false);
  assert.equal(services.settingsManager.getProviderRetrySettings().maxRetries, 0);
  writeFileSync(join(canonical, 'settings.json'), JSON.stringify({compaction:{enabled:false},
    retry:{enabled:true,maxRetries:88,provider:{maxRetries:88}}}));
  await services.settingsManager.reload();
  assert.equal(services.settingsManager.getCompactionSettings().enabled, false);
  assert.equal(services.settingsManager.getRetrySettings().maxRetries, 0);
  assert.equal(services.settingsManager.getProviderRetrySettings().maxRetries, 0);
  // Trust recalculation must not permit tracked retries, even after settings reload.
  const {AgentSession} = await import(pathToFileURL(join(pkg, 'dist/core/agent-session.js')));
  assert.equal(await AgentSession.prototype._checkCompaction.call({}, {}), false);
  console.log('canonical/project settings and tracked automatic-compaction isolation passed');
} finally {rmSync(root, {recursive:true,force:true});}
