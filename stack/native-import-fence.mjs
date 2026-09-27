// Deployment-root import boundary. Copied to dist/ and preloaded BEFORE any SDK module.
// Not a sandbox for trusted code, native addons, subprocesses, eval, or same-UID mutation.
import { lstatSync, readFileSync, realpathSync } from 'node:fs';
import { isBuiltin, registerHooks } from 'node:module';
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = realpathSync(resolve(dirname(fileURLToPath(import.meta.url)), '..'));
const deny = detail => Object.assign(new Error(`Native import boundary refused: ${detail}`),
    { code: 'ERR_NATIVE_IMPORT_BOUNDARY' });
const contained = path => {
    const tail = relative(root, path);
    return tail !== '' && tail !== '..' && !tail.startsWith(`..${sep}`) && !isAbsolute(tail);
};
function checkedFile(path) {
    const lexical = resolve(path);
    if (!contained(lexical)) throw deny('outside committed deployment root');
    const stat = lstatSync(lexical);
    if (!stat.isFile() || stat.nlink !== 1) throw deny('not an independent regular file');
    const actual = realpathSync(lexical);
    if (actual !== lexical || !contained(actual)) throw deny('noncanonical file path');
    return actual;
}
function manifestFile(path) {
    if (typeof path !== 'string' || path.length > 4096 || isAbsolute(path) ||
        path.split(/[\\/]/).some(part => part === '..' || part === '.' || part === ''))
        throw deny('invalid manifest-relative file');
    return checkedFile(resolve(root, path));
}
const manifestPath = checkedFile(resolve(root, 'dist/agent-comms-imports.json'));
if (lstatSync(manifestPath).size > 16384) throw deny('oversized import manifest');
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
if (manifest?.version !== 1 || Object.keys(manifest).sort().join(',') !==
    'extensionEntries,peerAliases,version' || !Array.isArray(manifest.extensionEntries) ||
    manifest.extensionEntries.length > 16 || !manifest.peerAliases ||
    typeof manifest.peerAliases !== 'object' || Array.isArray(manifest.peerAliases) ||
    Object.keys(manifest.peerAliases).length > 32)
    throw deny('invalid import manifest');
const entries = new Set(manifest.extensionEntries.map(entry => {
    if (typeof entry !== 'string' || !entry.endsWith('.mjs')) throw deny('native ESM extension required');
    return manifestFile(entry);
}));
if (entries.size !== manifest.extensionEntries.length) throw deny('duplicate extension entry');
const extensionRoots = [...entries].map(dirname);
const aliases = new Map(Object.entries(manifest.peerAliases).map(([name, target]) => {
    if (!/^@[a-z0-9-]+\/[a-z0-9-]+(?:\/[a-z0-9-]+)*$/.test(name)) throw deny('invalid peer alias');
    return [name, pathToFileURL(manifestFile(target)).href];
}));
function checkedURL(url) {
    if (isBuiltin(url)) return url;
    let parsed;
    try { parsed = new URL(url); } catch { throw deny('non-URL resolution'); }
    if (parsed.protocol !== 'file:') throw deny('non-file, non-builtin module');
    checkedFile(fileURLToPath(parsed));
    return url;
}
function extensionParent(url) {
    if (!url?.startsWith('file:')) return false;
    const parent = fileURLToPath(url);
    return extensionRoots.some(dir => parent.startsWith(`${dir}${sep}`));
}
if (typeof registerHooks !== 'function') throw deny('synchronous Node resolve/load hooks unavailable');
// Deliberately do not export a deregistration handle, allow-root override, or caller digest.
registerHooks({
    resolve(specifier, context, nextResolve) {
        const target = extensionParent(context.parentURL) && aliases.has(specifier)
            ? aliases.get(specifier) : specifier;
        const result = nextResolve(target, context);
        checkedURL(result.url);
        return result;
    },
    load(url, context, nextLoad) {
        checkedURL(url);
        return nextLoad(url, context);
    },
});

/** Before package discovery/install/update: never fetch or run lifecycle scripts
 * for a source that cannot be executed under this immutable deployment policy. */
export function assertApprovedPackage(path) {
    if (typeof path !== 'string') throw deny('mutable npm/git package source');
    const actual = realpathSync(path);
    if (!entries.has(actual) && !extensionRoots.includes(actual))
        throw deny('package source not in deployment manifest');
}

/** This deployment only admits prebuilt local packages. No package acquisition,
 * metadata probe, global-root discovery or lifecycle subprocess is necessary.
 * Guard the package manager's actual async/capture/sync spawn sinks as well as
 * source admission, not unrelated trusted tools or approved MCP subprocesses. */
export function denyPackageSubprocess() {
    throw deny('package subprocess disabled in immutable deployment');
}

/** SDK loader replacement: exact manifest entry, native import, NO Jiti fallback. */
export async function loadApprovedExtension(path) {
    // Resource discovery can preserve an outer installation-path alias; authority
    // is the exact canonical entry in the already verified deployment tree.
    const entry = checkedFile(realpathSync(path));
    if (!entries.has(entry)) throw deny('extension entry not in deployment manifest');
    return (await import(pathToFileURL(entry).href)).default;
}
