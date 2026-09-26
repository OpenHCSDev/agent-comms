// Run as --input-type=module --eval under the actual pinned import fence.
// Fake commands only; the Python runner imposes kernel network denial on all children.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { pathToFileURL } from 'node:url';

const [pkg, root, mode] = process.argv.slice(1);
assert.equal(process.env.PI_OFFLINE, '0');
const { DefaultPackageManager } = await import(pathToFileURL(join(pkg, 'dist/core/package-manager.js')));
const agent = join(root, 'agent'), project = join(root, 'project'), bin = join(root, 'bin');
for (const dir of [agent, project, bin]) mkdirSync(dir, { recursive: true });
const marker = join(root, 'commands.jsonl');
const npm = join(bin, 'npm.cjs'), git = join(bin, 'git');
const fake = `#!${process.execPath}
const fs = require('node:fs');
const args = process.argv.slice(2);
fs.appendFileSync(${JSON.stringify(marker)}, JSON.stringify({command:process.argv[1],args})+'\\n');
if (process.argv[1].endsWith('/git')) {
  if (args[0] === 'ls-remote') console.log('b'.repeat(40)+'\\tHEAD');
  else if (args.includes('--abbrev-ref')) console.log('origin/main');
  else console.log('a'.repeat(40));
} else console.log('"0.2.0"');
`;
for (const path of [npm, git]) writeFileSync(path, fake, { mode: 0o700, flag: 'wx' });
process.env.PATH = `${bin}:${process.env.PATH}`;
const reset = () => { if (existsSync(marker)) unlinkSync(marker); };
const calls = () => existsSync(marker) ? readFileSync(marker, 'utf8').trim().split('\n').map(JSON.parse) : [];
// Counterfactual controls: both real child executables can write their marker.
for (const [command, args] of [[process.execPath, [npm, 'view', 'fixture']], [git, ['ls-remote']]]) {
    const child = spawnSync(command, args, { encoding: 'utf8' });
    assert.equal(child.status, 0, child.stderr);
    assert.equal(calls().length, 1);
    reset();
}
let globals = [], projects = [];
let npmCommand = [process.execPath, npm];
const settings = {
    getGlobalSettings: () => ({ packages: globals }),
    getProjectSettings: () => ({ packages: projects }),
    getNpmCommand: () => npmCommand,
    // In-memory test double only: reach package admission instead of stopping
    // at the separate project-trust gate. No persisted trust/approval is written.
    isProjectTrusted: () => true,
};
const pm = new DefaultPackageManager({ cwd: project, agentDir: agent, settingsManager: settings });
const npmSource = 'npm:unapproved-fixture@^0.1.0';
const gitSource = 'git:github.com/fixture/unapproved';
const npmParsed = pm.parseSource(npmSource), gitParsed = pm.parseSource(gitSource);
assert.equal(gitParsed.type, 'git');
const installed = pm.getManagedNpmInstallPath(npmParsed, 'user');
mkdirSync(installed, { recursive: true });
writeFileSync(join(installed, 'package.json'), JSON.stringify({ name: 'unapproved-fixture', version: '0.1.0' }));
const gitInstalled = pm.getGitInstallPath(gitParsed, 'user');
mkdirSync(gitInstalled, { recursive: true });
const approved = join(pkg, 'agent-comms-extensions/pi-mcp-client');
const results = [];
async function denied(name, action, requireRejection = true) {
    reset();
    let error;
    try { await action(); } catch (caught) { error = caught; }
    assert.deepEqual(calls(), [], `${name}: package subprocess executed`);
    if (requireRejection) assert.equal(error?.code, 'ERR_NATIVE_IMPORT_BOUNDARY', `${name}: ${error}`);
    results.push({ name, refused: !!error, noSubprocess: true });
}
if (mode === 'old') {
    // Preserve old-byte proof: present installations reach the actual commands,
    // while the separately guarded real update rejects the same source.
    for (const source of [npmSource, gitSource]) {
        globals = [source]; projects = []; reset();
        assert.equal((await pm.checkForAvailableUpdates()).length, 1);
        const observed = calls();
        assert.ok(observed.some(call => call.args[0] === (source === npmSource ? 'view' : 'ls-remote')));
        results.push({ source, updateCheckCommands: observed });
        await denied(`old-update-${source}`, () => pm.updateConfiguredSources([{ source, scope: 'user' }]));
    }
} else {
    // Complete source-set admission: user/project, plain/filtered source entries,
    // approved-before-denied ordering, pinned source and absent-install variants.
    for (const source of [npmSource, gitSource, 'npm:unapproved-fixture@0.1.0', 'npm:missing-fixture']) {
        for (const scope of ['user', 'project']) {
            for (const filtered of [false, true]) {
                const entries = [approved, filtered ? { source, extensions: [] } : source];
                globals = scope === 'user' ? entries : [];
                projects = scope === 'project' ? entries : [];
                await denied(`check-${source}-${scope}-${filtered}`, () => pm.checkForAvailableUpdates());
            }
        }
    }
    globals = [approved]; projects = [];
    // The approved immutable source must remain usable, without any subprocess.
    await pm.install(approved);
    assert.deepEqual(await pm.checkForAvailableUpdates(), []);
    await pm.updateConfiguredSources([{ source: approved, scope: 'user' }]);
    globals = [relative(agent, approved)];
    assert.deepEqual(await pm.checkForAvailableUpdates(), []);
    const resolved = await pm.resolve();
    assert.ok(resolved.extensions.some(entry => entry.path === join(approved, 'index.mjs')));
    assert.deepEqual(calls(), []);
    results.push({ name: 'approved-install-relative-discovery-check-update', noSubprocess: true });
    // The SDK's helper methods are ordinary JS methods, not runtime-private.
    // Cover direct metadata/probe APIs and all actual async/capture/sync sinks.
    const direct = [
        ['npm-latest', () => pm.getLatestNpmVersion('unapproved-fixture')],
        ['npm-update-probe', () => pm.npmHasAvailableUpdate(npmParsed, installed), false],
        ['git-update-probe', () => pm.gitHasAvailableUpdate(gitInstalled), false],
        ['git-remote-head', () => pm.getRemoteGitHead(gitInstalled)],
        ['git-local-target', () => pm.getLocalGitUpdateTarget(gitInstalled)],
        ['git-upstream', () => pm.getGitUpstreamRef(gitInstalled), false],
        ['git-remote-command', () => pm.runGitRemoteCommand(gitInstalled, ['ls-remote', 'origin'])],
        ['global-npm-root', () => pm.getGlobalNpmRoot()],
        ['legacy-global-path', () => pm.getLegacyGlobalNpmInstallPath(npmParsed), false],
        ['missing-npm-path', () => pm.getNpmInstallPath(pm.parseSource('npm:missing-fixture'), 'user'), false],
        ['npm-command', () => pm.runNpmCommand(['view', 'unapproved-fixture'])],
        ['npm-command-sync', () => pm.runNpmCommandSync(['root', '-g'])],
        ['spawn', () => pm.spawnCommand(process.execPath, [npm, 'view'])],
        ['spawn-capture', () => pm.spawnCaptureCommand(process.execPath, [npm, 'view'])],
        ['run-command', () => pm.runCommand(process.execPath, [npm, 'view'])],
        ['run-capture', () => pm.runCommandCapture(process.execPath, [npm, 'view'])],
        ['run-sync', () => pm.runCommandSync(process.execPath, [npm, 'view'])],
    ];
    for (const [name, action, rejection = true] of direct) await denied(name, action, rejection);
    for (const flavor of ['pnpm', 'bun']) {
        const command = join(bin, flavor);
        writeFileSync(command, fake, { mode: 0o700, flag: 'wx' });
        npmCommand = [command];
        assert.equal(pm.getPackageManagerName(), flavor);
        await denied(`${flavor}-global-discovery`, () => flavor === 'pnpm'
            ? pm.getPnpmGlobalPackagePath('unapproved-fixture') : pm.getGlobalNpmRoot());
    }
}
reset();
console.log(JSON.stringify({ ok: true, mode, cases: results.length, results }));
