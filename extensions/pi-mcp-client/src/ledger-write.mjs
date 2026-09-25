import { randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { mkdir, open, realpath, rename, rm, rmdir } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import { parseTrustLedger } from './authority.mjs';
import { declarationDigest } from './config.mjs';
import { readOptional } from './sources.mjs';

const EMPTY_LEDGER = '{"version":1,"decisions":[]}';

/** Persist an explicitly user-authorized decision. Call ONLY after an independent human confirmation. */
export async function recordProjectDecision({ agentDir, projectRoot, declaration, decision }) {
  if (!isAbsolute(agentDir) || !isAbsolute(projectRoot)) {
    throw new Error('Absolute Pi agent directory and project root required');
  }
  // The reader keys by the canonical project path. The caller must present this path
  // and the exact declaration to the user BEFORE invoking this method.
  const canonicalRoot = await realpath(projectRoot);
  const digest = declarationDigest(declaration);
  const row = { projectRoot: canonicalRoot, serverId: declaration.id, digest, decision };
  parseTrustLedger(JSON.stringify({ version: 1, decisions: [row] }));
  const ledgerPath = join(agentDir, 'mcp-trust.json');
  const lockPath = ledgerPath + '.lock';
  // An existing lock is a hard error, including after a crash; never guess
  // whether another process owns it or silently remove a stale lock.
  await mkdir(lockPath, { mode: 0o700 });
  let temp;
  try {
    const current = parseTrustLedger(await readOptional(ledgerPath) ?? EMPTY_LEDGER);
    // A new decision for an ID revokes EVERY prior digest for that project/ID,
    // preventing an old approved project declaration from becoming active by rollback.
    const decisions = current.decisions.filter((entry) =>
      entry.projectRoot !== canonicalRoot || entry.serverId !== declaration.id);
    decisions.push(row);
    const serialized = JSON.stringify(parseTrustLedger(JSON.stringify({ version: 1, decisions }))) + '\n';
    temp = join(agentDir, `.mcp-trust-${randomUUID()}.tmp`);
    const file = await open(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
    try {
      await file.writeFile(serialized, 'utf8');
      await file.sync();
    } finally {
      await file.close();
    }
    await rename(temp, ledgerPath);
    temp = undefined;
    if (process.platform !== 'win32') {
      try {
        const directory = await open(agentDir, constants.O_RDONLY);
        try { await directory.sync(); } finally { await directory.close(); }
      } catch (error) {
        // The rename already happened. Do not report a clean durable commit.
        throw new Error('MCP decision was written but directory durability is unknown', { cause: error });
      }
    }
    return { projectRoot: canonicalRoot, serverId: declaration.id, digest, decision };
  } finally {
    if (temp) await rm(temp, { force: true });
    await rmdir(lockPath);
  }
}
