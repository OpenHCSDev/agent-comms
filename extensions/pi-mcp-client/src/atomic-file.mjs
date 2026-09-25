import { randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { mkdir, open, rename, rm, rmdir } from 'node:fs/promises';
import { dirname, isAbsolute, join } from 'node:path';
import { readOptional } from './sources.mjs';

/** One package-owned atomic replacement primitive for config and decision stores. */
export async function updateJsonFile(path, { initial, parse, mode, update }) {
  if (!isAbsolute(path)) throw new Error('Absolute MCP store path required');
  const lock = path + '.lock';
  await mkdir(lock, { mode: 0o700 }); // Existing/stale lock is a hard error.
  let temp;
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
    await rename(temp, path);
    temp = undefined;
    if (process.platform !== 'win32') {
      try {
        const directory = await open(dirname(path), constants.O_RDONLY);
        try { await directory.sync(); } finally { await directory.close(); }
      } catch (error) {
        throw new Error('MCP store was written but directory durability is unknown', { cause: error });
      }
    }
    return next;
  } finally {
    if (temp) await rm(temp, { force: true });
    await rmdir(lock);
  }
}
