import { realpath } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import { parseTrustLedger } from './authority.mjs';
import { updateJsonFile } from './atomic-file.mjs';
import { declarationDigest } from './config.mjs';

const EMPTY_LEDGER = '{"version":1,"decisions":[],"callGrants":[]}';

async function updateLedger(agentDir, update) {
  if (!isAbsolute(agentDir)) throw new Error('Absolute Pi agent directory required');
  return updateJsonFile(join(agentDir, 'mcp-trust.json'), {
    initial: EMPTY_LEDGER, parse: parseTrustLedger, mode: 0o600, update,
    durableAuthority: true,
  });
}

/** Persist an explicitly user-authorized declaration decision. Never call from project data. */
export async function recordProjectDecision({ agentDir, projectRoot, declaration, decision }) {
  if (!isAbsolute(projectRoot)) throw new Error('Absolute project root required');
  const canonicalRoot = await realpath(projectRoot);
  const row = { projectRoot: canonicalRoot, scope: 'project', serverId: declaration.id,
    digest: declarationDigest(declaration), decision };
  parseTrustLedger(JSON.stringify({ version: 1, decisions: [row] }));
  await updateLedger(agentDir, (current) => ({ ...current,
    decisions: [...current.decisions.filter((entry) =>
      entry.projectRoot !== canonicalRoot || entry.serverId !== declaration.id), row],
    // A launch denial also retires any previous autonomous call grant for
    // this project/server. Reapproval must not silently revive an old grant.
    callGrants: decision === 'deny' ? current.callGrants.filter((entry) =>
      entry.projectRoot !== canonicalRoot || entry.scope !== 'project' ||
      entry.serverId !== declaration.id) : current.callGrants,
  }));
  return row;
}

/** Separate human grant for autonomous calls; a changed declaration invalidates it. */
export async function recordCallGrant({ agentDir, entry, decision }) {
  const row = { projectRoot: entry.projectRoot, scope: entry.scope,
    serverId: entry.declaration.id, digest: entry.digest, decision };
  parseTrustLedger(JSON.stringify({ version: 1, decisions: [], callGrants: [row] }));
  await updateLedger(agentDir, (current) => {
    // Check the approval INSIDE the same ledger writer lock as the grant.
    // A concurrent deny between the caller's approved snapshot and this
    // mutation must not recreate an autonomous grant that a later approval
    // of the same digest would revive.
    if (row.scope === 'project' && !current.decisions.some((decision) =>
      decision.projectRoot === row.projectRoot && decision.scope === 'project' &&
      decision.serverId === row.serverId && decision.digest === row.digest &&
      decision.decision === 'approve')) {
      throw new Error('MCP project approval changed before call grant commit');
    }
    return { ...current,
      callGrants: [...current.callGrants.filter((existing) =>
        existing.projectRoot !== row.projectRoot || existing.scope !== row.scope ||
        existing.serverId !== row.serverId), row],
    };
  });
  return row;
}
