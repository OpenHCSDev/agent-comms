import { randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { mkdir, open, rename, rm, rmdir } from 'node:fs/promises';
import { dirname, isAbsolute, join } from 'node:path';
import { readOptional } from './sources.mjs';

export async function syncParentDirectory(path) {
  if (process.platform === 'win32') return;
  const directory = await open(dirname(path), constants.O_RDONLY);
  try { await directory.sync(); } finally { await directory.close(); }
}

/** Atomic config replacement; decision stores additionally use a durable uncertainty marker. */
export async function updateJsonFile(path, { initial, parse, mode, update,
  durableAuthority = false, syncDirectory = syncParentDirectory }) {
  if (!isAbsolute(path)) throw new Error('Absolute MCP store path required');
  // Node cannot fsync a Windows parent directory; never report a durable
  // approval or revocation on that platform without a proven strategy.
  if (durableAuthority && process.platform === 'win32') {
    throw new Error('Durable MCP decision writes are unavailable on Windows');
  }
  const lock = path + '.lock';
  const unsafe = path + '.unsafe';
  await mkdir(lock, { mode: 0o700 }); // Existing/stale lock is a hard error.
  let temp;
  let lockReleased = false;
  try {
    const current = parse(await readOptional(path) ?? initial);
    const next = parse(JSON.stringify(update(current)));
    temp = join(dirname(path), `.mcp-${randomUUID()}.tmp`);
    const file = await open(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, mode);
    try {
      await file.writeFile(JSON.stringify(next) + '\n', 'utf8');
      await file.sync();
    } finally {
      await file.close();
    }
    if (durableAuthority) {
      // The marker is durable BEFORE the rename. On any crash/error until the
      // new ledger's directory fsync succeeds, readers refuse all grants.
      await mkdir(unsafe, { mode: 0o700 });
      await syncDirectory(path);
    }
    await rename(temp, path);
    temp = undefined;
    try {
      await syncDirectory(path);
    } catch (error) {
      throw new Error('MCP store was written but directory durability is unknown', { cause: error });
    }
    if (durableAuthority) {
      // A failed lock cleanup must not look like a failed approval while the
      // approval is active. Keep the marker until cleanup succeeds.
      await rmdir(lock);
      lockReleased = true;
      await rmdir(unsafe);
      // The ledger rename already survived fsync. If marker-removal fsync
      // fails, return success: after a crash the marker either reappears
      // (fail closed) or the durable ledger remains (authorized success).
      await syncDirectory(path).catch(() => {});
    }
    return next;
  } finally {
    if (temp) await rm(temp, { force: true });
    if (!lockReleased) await rmdir(lock);
  }
}
