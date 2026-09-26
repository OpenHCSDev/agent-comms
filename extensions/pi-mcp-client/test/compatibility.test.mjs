import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { cp, mkdir, mkdtemp, readFile, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';
import { ProjectTrustStore } from '@earendil-works/pi-coding-agent';
import { writeNativeServer } from '../src/config-write.mjs';
import { loadEffectiveDeclarations, readTrustLedger } from '../src/sources.mjs';
import { callGrantDecision } from '../src/authority.mjs';
import * as fixedWriter from '../src/ledger-write.mjs';

const packageRoot = join(import.meta.dirname, '..');
const capability = { version: 1, positiveDecisions: 'locked-project-approval-v1' };

test('identical CLI bytes do not qualify legacy imported authority for positive decisions',
  { skip: process.platform === 'win32' && 'Windows durable decision writes are disabled' }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'mcp-compatibility-'));
  try {
    // Reconstitute the two historical modules verbatim from b6602e5. Other
    // ledger/config dependencies are unchanged since that commit. Never run
    // a server or a positive CLI challenge through this vulnerable fixture.
    const legacy = join(root, 'legacy');
    await cp(join(packageRoot, 'src'), join(legacy, 'src'), { recursive: true });
    await cp(join(packageRoot, 'bin'), join(legacy, 'bin'), { recursive: true });
    await symlink(join(packageRoot, 'node_modules'), join(legacy, 'node_modules'), 'dir');
    for (const name of ['inventory', 'ledger-write']) {
      await cp(join(import.meta.dirname, 'fixtures', `b6602e5-${name}.mjs.txt`),
        join(legacy, 'src', `${name}.mjs`));
    }
    const cli = join(packageRoot, 'bin', 'pi-mcp.mjs');
    const legacyCli = join(legacy, 'bin', 'pi-mcp.mjs');
    assert.deepEqual(await readFile(cli), await readFile(legacyCli));
    const legacyWriter = await import(pathToFileURL(join(legacy, 'src', 'ledger-write.mjs')));
    for (const [label, executable, writer] of [
      ['fixed', cli, fixedWriter], ['legacy', legacyCli, legacyWriter],
    ]) {
      const home = join(root, label + '-home');
      const project = join(home, 'project');
      const agentDir = join(home, 'agent');
      await mkdir(project, { recursive: true });
      new ProjectTrustStore(agentDir).set(project, true);
      const declaration = { id: 'fixture', enabled: true, instructionsPolicy: 'status-only',
        transport: { type: 'stdio', command: 'never-launched', args: [], cwd: 'project' } };
      await writeNativeServer({ agentDir, projectRoot: project, configDirName: '.pi',
        scope: 'project', declaration });
      const inventory = () => {
        const result = spawnSync(process.execPath, [executable, 'inventory', '--json'], {
          cwd: project, env: { PATH: process.env.PATH ?? '', HOME: home,
            PI_CODING_AGENT_DIR: agentDir, CI: 'true', NO_COLOR: '1' },
          encoding: 'utf8', timeout: 5000,
        });
        assert.equal(result.status, 0, result.stderr);
        return JSON.parse(result.stdout);
      };
      const before = inventory();
      assert.equal(before.version, 2);
      assert.deepEqual(before.compatibility, label === 'fixed' ? capability : undefined);
      // The compatibility field promises implementation semantics, not an
      // approval: it is present even when this declaration requires trust.
      assert.equal(before.declarations.project[0].status, 'trust_required');
      const options = { agentDir, configDirName: '.pi',
        ctx: { cwd: project, isProjectTrusted: () => true } };
      const decide = (decision) => writer.recordProjectDecision({ agentDir,
        projectRoot: project, declaration, decision });
      await decide('approve');
      const entry = (await loadEffectiveDeclarations(options))[0];
      await decide('deny');
      const staleGrant = () => writer.recordCallGrant({ agentDir, entry, decision: 'allow' });
      if (label === 'fixed') {
        await assert.rejects(staleGrant(), /approval changed before call grant commit/);
      } else {
        await staleGrant(); // Historical bug: identical entrypoint, different authority.
      }
      await decide('approve');
      const current = (await loadEffectiveDeclarations(options))[0];
      assert.equal(callGrantDecision(await readTrustLedger(agentDir), current),
        label === 'fixed' ? 'ask' : 'allow');
      assert.deepEqual(inventory().compatibility, label === 'fixed' ? capability : undefined);
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
