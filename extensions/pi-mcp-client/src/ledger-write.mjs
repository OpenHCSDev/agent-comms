import { randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { mkdir, open, realpath, rename, rm, rmdir } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import { parseTrustLedger } from './authority.mjs';
import { declarationDigest } from './config.mjs';
import { readOptional } from './sources.mjs';

const EMPTY_LEDGER = '{"version":1,"decisions":[],"callGrants":[]}';

async function updateLedger(agentDir, update) {
  if (!isAbsolute(agentDir)) throw new Error('Absolute Pi agent directory required');
  const path = join(agentDir, 'mcp-trust.json');
  const lock = path + '.lock';
  // An existing lock, including one left by a crash, fails closed.
  await mkdir(lock, { mode: 0o700 });
  let temp;
  try {
    const current = parseTrustLedger(await readOptional(path) ?? EMPTY_LEDGER);
    const next = parseTrustLedger(JSON.stringify(update(current)));
    temp = join(agentDir, `.mcp-trust-${randomUUID()}.tmp`);
    const file = await open(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
    try {
      await file.writeFile(JSON.stringify(next) + '\n', 'utf8');
      await file.sync();
    } finally {
      await file.close();
    }
    await rename(temp, path);
    temp = undefined;
    if (process.platform !== 'win32') {
      try {
        const directory = await open(agentDir, constants.O_RDONLY);
        try { await directory.sync(); } finally { await directory.close(); }
      } catch (error) {
        throw new Error('MCP decision was written but directory durability is unknown', { cause: error });
      }
    }
  } finally {
    if (temp) await rm(temp, { force: true });
    await rmdir(lock);
  }
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
  }));
  return row;
}

/** Separate human grant for autonomous calls; a changed declaration invalidates it. */
export async function recordCallGrant({ agentDir, entry, decision }) {
  const row = { projectRoot: entry.projectRoot, scope: entry.scope,
    serverId: entry.declaration.id, digest: entry.digest, decision };
  parseTrustLedger(JSON.stringify({ version: 1, decisions: [], callGrants: [row] }));
  await updateLedger(agentDir, (current) => ({ ...current,
    callGrants: [...current.callGrants.filter((existing) =>
      existing.projectRoot !== row.projectRoot || existing.scope !== row.scope ||
      existing.serverId !== row.serverId), row],
  }));
  return row;
}
