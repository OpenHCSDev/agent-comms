import {lstatSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {package:root, cwd, context_tokens:contextTokens, context_window:contextWindow} = JSON.parse(process.argv[1]);
const {CONFIG_DIR_NAME, getAgentDir} = await import(pathToFileURL(join(root, 'dist/config.js')));
const {SettingsManager} = await import(
  pathToFileURL(join(root, 'dist/core/settings-manager.js')));
const {shouldCompact} = await import(
  pathToFileURL(join(root, 'dist/core/compaction/compaction.js')));
// Pi's migration, merge and effective defaults are authoritative. Its normal
// FileSettingsStorage takes and writes lock files even for reads; this custom
// storage exposes *only* immutable read callbacks, and refuses write attempts.
const storage = {withLock(scope, callback) {
  const path = scope === 'global' ? join(getAgentDir(), 'settings.json') :
    scope === 'project' ? join(cwd, CONFIG_DIR_NAME, 'settings.json') : null;
  if (!path) throw new Error('Invalid settings scope');
  let raw;
  try {
    const before = lstatSync(path, {bigint:true});
    if (!before.isFile() || before.nlink !== 1n || before.size > 1048576n)
      throw new Error('Settings must be a bounded regular file');
    raw = readFileSync(path, 'utf8');
    const after = lstatSync(path, {bigint:true});
    if (before.dev !== after.dev || before.ino !== after.ino ||
        before.size !== after.size || before.mtimeNs !== after.mtimeNs ||
        before.ctimeNs !== after.ctimeNs)
      throw new Error('Settings changed during read');
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
  if (callback(raw) !== undefined) throw new Error('Settings read attempted a write');
}};
const manager = SettingsManager.fromStorage(storage, {projectTrusted:true});
if (manager.globalSettingsLoadError || manager.projectSettingsLoadError)
  throw new Error('Pi settings cannot be loaded without fallback');
const settings = manager.getCompactionSettings();
console.log(JSON.stringify({enabled:settings.enabled,
  reserveTokens:settings.reserveTokens, keepRecentTokens:settings.keepRecentTokens,
  trigger:shouldCompact(contextTokens,contextWindow,settings)}));
