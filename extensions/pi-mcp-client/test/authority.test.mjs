import assert from 'node:assert/strict';
import { test } from 'node:test';
import { effectiveDeclarations, parseTrustLedger } from '../src/authority.mjs';
import { declarationDigest, parseNativeConfig } from '../src/config.mjs';

const server = (command, id = 'one') => ({
  id, enabled: true, instructionsPolicy: 'status-only',
  transport: { type: 'stdio', command, args: [], cwd: 'project' },
});
const config = (...servers) => parseNativeConfig(JSON.stringify({ version: 1, servers }));
const ledger = (...decisions) => parseTrustLedger(JSON.stringify({ version: 1, decisions }));
const projectRoot = '/canonical/project';
const decide = (user, project, approvals, projectTrusted = true, root = projectRoot) =>
  effectiveDeclarations({ user, project, ledger: approvals, projectTrusted, projectRoot: root });

test('project authority requires Pi trust AND separately sourced exact digest', () => {
  const declaration = server('project-command');
  const user = config(server('user-command'));
  const project = config(declaration);
  const approval = { projectRoot, scope: 'project', serverId: 'one', digest: declarationDigest(project.servers[0]), decision: 'approve' };
  assert.deepEqual(decide(user, project, ledger()).map((entry) => [entry.scope, entry.status]),
    [['project', 'trust_required']]); // Never silently fall back to the user server.
  assert.deepEqual(decide(user, project, ledger(approval)).map((entry) => [entry.scope, entry.status]),
    [['project', 'approved']]);
  assert.equal(decide(user, config(server('changed-command')), ledger(approval))[0].status, 'trust_required');
  assert.equal(decide(user, project, ledger(approval), true, '/another/project')[0].status, 'trust_required');
  assert.equal(decide(user, project, ledger({ ...approval, decision: 'deny' }))[0].status, 'denied');
  assert.throws(() => decide(user, project, ledger(approval), false), /must not be read/);
  assert.deepEqual(decide(user, undefined, ledger(approval), false).map((entry) => [entry.scope, entry.status]),
    [['user', 'approved']]);
});

test('ledger rejects malformed or duplicated decisions without leaking fields', () => {
  const decision = { projectRoot, scope: 'project', serverId: 'one', digest: 'a'.repeat(64), decision: 'approve' };
  for (const raw of [
    '{"version":1,"decisions":[{"secret":"never-expose-me"}',
    JSON.stringify({ version: 1, decisions: [decision, decision] }),
    JSON.stringify({ version: 1, decisions: [{ ...decision, trust: true }] }),
    JSON.stringify({ version: 1, decisions: [{ ...decision, scope: 'user' }] }),
    JSON.stringify({ version: 1, decisions: [{ ...decision, digest: 'never-expose-me' }] }),
  ]) {
    assert.throws(() => parseTrustLedger(raw), (error) =>
      error.message.startsWith('Invalid MCP trust ledger:') && !error.message.includes('never-expose-me'));
  }
});

test('user declarations and disabled project overlays have explicit precedence', () => {
  const user = config(server('user-one'), server('user-two', 'two'));
  const project = config({ ...server('project-one'), enabled: false });
  const result = decide(user, project, ledger());
  assert.deepEqual(result.map(({ declaration, scope, status }) => [declaration.id, scope, status]), [
    ['one', 'project', 'disabled'], ['two', 'user', 'approved'],
  ]);
});
