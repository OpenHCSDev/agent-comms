// Isolated contract tests for the draft boundary module; NOT canonical/PR77 E2E clearance.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const source = dirname(fileURLToPath(import.meta.url));
const base = fs.mkdtempSync(join(tmpdir(), 'pr95-import-boundary-'));
const root = join(base, 'deployment');
const dist = join(root, 'dist');
const entry = join(root, 'extensions/approved/index.mjs');
function put(path, data) { fs.mkdirSync(dirname(path), { recursive: true }); fs.writeFileSync(path, data); }
put(join(root, 'package.json'), '{"type":"module"}');
put(join(dist, 'agent-comms-import-fence.mjs'), fs.readFileSync(join(source, 'native-import-fence.mjs')));
put(join(dist, 'index.js'), 'export const identity = "committed-host-sdk";');
put(entry, 'import {identity} from "@earendil-works/pi-coding-agent"; export default () => identity;');
const sourceEntry = join(base, 'user-extension/index.ts');
const sourceDependency = join(base, 'user-extension/dependency.ts');
const snapshot = join(root, 'extensions/snapshot/index.mjs');
put(sourceEntry, 'export {default} from "./dependency.ts";');
put(sourceDependency, 'export default () => "source-snapshot";');
put(snapshot, 'export default () => "source-snapshot";');
const sourceRecords = [sourceEntry, sourceDependency].map(path => ({path,
    bytes: fs.statSync(path).size, sha256: createHash('sha256').update(fs.readFileSync(path)).digest('hex'),
    snapshot: 'fixture-snapshot'}));
put(join(dist, 'agent-comms-imports.json'), JSON.stringify({ version: 2,
    extensionEntries: [{entry: 'extensions/approved/index.mjs'},
        {entry: 'extensions/snapshot/index.mjs', sources: sourceRecords}],
    peerAliases: { '@earendil-works/pi-coding-agent': 'dist/index.js' } }));
const sentinel = join(base, 'node_modules/ancestor-sentinel/index.cjs');
put(join(dirname(sentinel), 'package.json'), '{"name":"ancestor-sentinel","main":"index.cjs"}');
put(sentinel, 'require("node:fs").writeFileSync(process.env.SENTINEL_MARKER,"executed"); module.exports = 1;');
let sequence = 0;
function run(code, fenced = true, suffix = 'mjs') {
    const marker = join(base, `marker-${sequence}`), probe = join(dist, `probe-${sequence++}.${suffix}`);
    put(probe, code);
    const env = { ...process.env, SENTINEL_MARKER: marker };
    delete env.NODE_OPTIONS; delete env.NODE_PATH;
    const args = ['--no-global-search-paths'];
    if (fenced) args.push('--import', join(dist, 'agent-comms-import-fence.mjs'));
    const result = spawnSync(process.execPath, [...args, probe], { env, encoding: 'utf8', timeout: 15000 });
    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stderr);
    return { output: result.stdout.trim(), executed: fs.existsSync(marker) };
}
const esm = 'try { await import("ancestor-sentinel"); console.log("allowed"); } catch(e) { console.log(e.code); }';
assert.deepEqual(run(esm, false), { output: 'allowed', executed: true }, 'old no-global flag permits ancestor execution');
assert.deepEqual(run(esm), { output: 'ERR_NATIVE_IMPORT_BOUNDARY', executed: false });
const cjs = 'try { require("ancestor-sentinel"); console.log("allowed"); } catch(e) { console.log(e.code); }';
assert.deepEqual(run(cjs, true, 'cjs'), { output: 'ERR_NATIVE_IMPORT_BOUNDARY', executed: false });
assert.deepEqual(run('import {createRequire} from "node:module"; const require = createRequire(import.meta.url);' + cjs),
    { output: 'ERR_NATIVE_IMPORT_BOUNDARY', executed: false });
assert.deepEqual(run(`try { await import(${JSON.stringify(sentinel)}); } catch(e) { console.log(e.code); }`),
    { output: 'ERR_NATIVE_IMPORT_BOUNDARY', executed: false });
assert.deepEqual(run('try { await import("data:text/javascript,export default 1"); } catch(e) { console.log(e.code); }'),
    { output: 'ERR_NATIVE_IMPORT_BOUNDARY', executed: false });
assert.deepEqual(run(`import {loadApprovedExtension} from './agent-comms-import-fence.mjs';
    console.log((await loadApprovedExtension(${JSON.stringify(entry)}))());`),
    { output: 'committed-host-sdk', executed: false });
const sourceProbe = `import {loadApprovedExtension} from './agent-comms-import-fence.mjs';
    try { console.log((await loadApprovedExtension(${JSON.stringify(sourceEntry)}))()); }
    catch(e) { console.log(e.code); }`;
assert.deepEqual(run(sourceProbe), { output: 'source-snapshot', executed: false });
put(sourceDependency, 'export default () => "changed-content";');
assert.deepEqual(run(sourceProbe), { output: 'ERR_NATIVE_IMPORT_BOUNDARY', executed: false });
put(sourceDependency, 'export default () => "source-snapshot";');
// A same-content unlisted entry is not granted authority by the source digest.
const unlistedSource = join(base, 'user-extension/unlisted.ts');
put(unlistedSource, fs.readFileSync(sourceEntry));
assert.deepEqual(run(`import {loadApprovedExtension} from './agent-comms-import-fence.mjs';
    try { await loadApprovedExtension(${JSON.stringify(unlistedSource)}); }
    catch(e) { console.log(e.code); }`), { output: 'ERR_NATIVE_IMPORT_BOUNDARY', executed: false });
fs.unlinkSync(sourceDependency);
fs.symlinkSync(unlistedSource, sourceDependency);
assert.deepEqual(run(sourceProbe), { output: 'ERR_NATIVE_IMPORT_BOUNDARY', executed: false });
fs.unlinkSync(sourceDependency);
put(sourceDependency, 'export default () => "source-snapshot";');
const other = join(root, 'extensions/approved/not-listed.mjs');
put(other, 'throw new Error("must not run unlisted factory");');
assert.deepEqual(run(`import {loadApprovedExtension} from './agent-comms-import-fence.mjs';
    try { await loadApprovedExtension(${JSON.stringify(other)}); } catch(e) { console.log(e.code); }`),
    { output: 'ERR_NATIVE_IMPORT_BOUNDARY', executed: false });
// Authorized entry reaches its dependency import. Failure is not missing entry approval.
put(entry, 'import "ancestor-sentinel"; export default () => {};');
assert.deepEqual(run(`import {loadApprovedExtension} from './agent-comms-import-fence.mjs';
    try { await loadApprovedExtension(${JSON.stringify(entry)}); } catch(e) { console.log(e.code); }`),
    { output: 'ERR_NATIVE_IMPORT_BOUNDARY', executed: false });
// A native evaluation failure propagates; this API never retries via a CJS/Jiti evaluator.
put(entry, 'if (typeof module === "undefined") throw Object.assign(new Error("native-only"), {code:"NATIVE_FAILURE"});\n'
    + 'require("ancestor-sentinel"); export default () => {};');
assert.deepEqual(run(`import {loadApprovedExtension} from './agent-comms-import-fence.mjs';
    try { await loadApprovedExtension(${JSON.stringify(entry)}); } catch(e) { console.log(e.code); }`),
    { output: 'NATIVE_FAILURE', executed: false });
let cases = 14;
if (process.env.PI_NATIVE_PACKAGE_DIR) {
    // Copy the pinned Jiti dependency; never let its evaluator/cache touch a frozen artifact.
    const vendor = join(base, 'old-jiti');
    fs.cpSync(join(process.env.PI_NATIVE_PACKAGE_DIR, 'node_modules/jiti'), vendor,
        { recursive: true, dereference: true });
    for (const tryNative of [false, true]) {
        assert.deepEqual(run(`import {createJiti} from ${JSON.stringify(join(vendor, 'lib/jiti-static.mjs'))};
            const jiti = createJiti(import.meta.url, {moduleCache:false, fsCache:false, tryNative:${tryNative}});
            console.log(typeof await jiti.import(${JSON.stringify(entry)}, {default:true}));`, false),
            { output: 'function', executed: true }, 'old evaluator/fallback reaches ancestor sentinel');
        cases++;
    }
}
console.log(JSON.stringify({ ok: true, cases, base,
    scope: 'draft module only; canonical launcher integration and real PR77 fixture still required' }));
