// Bundle the lock-resolved pi-clm package into the immutable native package.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const packageRoot = resolve(process.argv[2]);
const sourceRoot = resolve(process.argv[3]);
const metadata = JSON.parse(readFileSync(resolve(sourceRoot, 'package.json'), 'utf8'));
const { build } = await import(pathToFileURL(resolve(packageRoot, 'node_modules/esbuild/lib/main.js')));
const result = await build({
    absWorkingDir: sourceRoot,
    entryPoints: ['index.ts'],
    bundle: true,
    write: false,
    platform: 'node',
    format: 'esm',
    target: 'node22',
    charset: 'utf8',
    banner: { js: 'process.env.PI_CLM_NATIVE_COMPACTION ??= "off";' },
    external: Object.keys(metadata.peerDependencies),
});
if (result.outputFiles.length !== 1) throw new Error('pi-clm must build as one native extension');
const destination = resolve(packageRoot, 'agent-comms-extensions/pi-clm/src/index.mjs');
mkdirSync(dirname(destination), { recursive: true, mode: 0o700 });
writeFileSync(destination, result.outputFiles[0].contents, { flag: 'wx', mode: 0o600 });
const steering = resolve(packageRoot, 'agent-comms-extensions/pi-clm/steering/house-brief.md');
mkdirSync(dirname(steering), { recursive: true, mode: 0o700 });
writeFileSync(steering, readFileSync(resolve(sourceRoot, 'steering/house-brief.md')), {
    flag: 'wx', mode: 0o600,
});
