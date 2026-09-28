// Build declared snapshots without evaluating mutable user source or its imports.
// All local inputs are enumerated on their owning extension declaration. Bare
// package peers remain external and resolve under the existing runtime fence.
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, extname, isAbsolute, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { isBuiltin } from 'node:module';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const packageRoot = resolve(process.argv[2]);
const { build } = await import(pathToFileURL(resolve(packageRoot, 'node_modules/esbuild/lib/main.js')));
const manifest = JSON.parse(readFileSync(resolve(repo, 'stack/native-import-manifest.json')));
if (manifest.version !== 2) throw new Error('Current extension declaration format required');
for (const declaration of manifest.extensionEntries) {
    if (!declaration.sources) continue;
    const sources = new Map(declaration.sources.map(source => {
        const snapshot = resolve(repo, source.snapshot);
        if (!snapshot.startsWith(`${repo}/`)) throw new Error('Source snapshot escapes repository');
        const contents = readFileSync(snapshot, 'utf8');
        if (Buffer.byteLength(contents) !== source.bytes ||
            createHash('sha256').update(contents).digest('hex') !== source.sha256)
            throw new Error(`Snapshot differs from declared source: ${source.snapshot}`);
        return [source.path, contents];
    }));
    const used = new Set();
    const result = await build({
        entryPoints: [declaration.sources[0].path], bundle: true, write: false,
        platform: 'node', format: 'esm', target: 'node22', charset: 'utf8',
        plugins: [{ name: 'declared-snapshot-inputs', setup(build) {
            build.onResolve({ filter: /.*/ }, args => {
                if (isBuiltin(args.path) || (!isAbsolute(args.path) && !args.path.startsWith('.')))
                    return { path: args.path, external: true };
                const path = resolve(dirname(args.importer || declaration.sources[0].path), args.path);
                if (!sources.has(path)) throw new Error(`Undeclared extension input: ${path}`);
                return { path, namespace: 'declared-source' };
            });
            build.onLoad({ filter: /.*/, namespace: 'declared-source' }, args => {
                used.add(args.path);
                return { contents: sources.get(args.path), loader: extname(args.path) === '.ts' ? 'ts' : 'js' };
            });
        }}],
    });
    if (used.size !== sources.size) throw new Error('Unused source inventory is not a complete build receipt');
    const destination = resolve(packageRoot, declaration.entry);
    if (!destination.startsWith(`${packageRoot}/`)) throw new Error('Entry escapes package');
    mkdirSync(dirname(destination), { recursive: true, mode: 0o700 });
    writeFileSync(destination, result.outputFiles[0].contents, { flag: 'wx', mode: 0o600 });
}
