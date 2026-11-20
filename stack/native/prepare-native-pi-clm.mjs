// Bundle the pinned pi-clm submodule into the immutable native package.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const stack = dirname(fileURLToPath(import.meta.url));
const repo = resolve(stack, '../..');
const packageRoot = resolve(process.argv[2]);
const sourceRoot = resolve(repo, 'extensions/pi-clm');
const metadata = JSON.parse(readFileSync(resolve(sourceRoot, 'package.json'), 'utf8'));
const { build } = await import(pathToFileURL(resolve(packageRoot, 'node_modules/esbuild/lib/main.js')));
const result = await build({
    entryPoints: [resolve(sourceRoot, 'index.ts')],
    bundle: true,
    write: false,
    platform: 'node',
    format: 'esm',
    target: 'node22',
    charset: 'utf8',
    external: Object.keys(metadata.peerDependencies),
});
if (result.outputFiles.length !== 1) throw new Error('pi-clm must build as one native extension');
const destination = resolve(packageRoot, 'agent-comms-extensions/pi-clm/src/index.mjs');
mkdirSync(dirname(destination), { recursive: true, mode: 0o700 });
writeFileSync(destination, result.outputFiles[0].contents, { flag: 'wx', mode: 0o600 });
