import {realpathSync, lstatSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {package:root, file, settings} = JSON.parse(process.argv[1]);
const {loadEntriesFromFile, SessionManager} = await import(
  pathToFileURL(join(root, 'dist/core/session-manager.js')));
const {prepareCompaction, DEFAULT_COMPACTION_SETTINGS} = await import(
  pathToFileURL(join(root, 'dist/core/compaction/compaction.js')));
const rows = loadEntriesFromFile(file);
if (!rows.length || rows[0].type !== 'session' || rows[0].version !== 3 ||
    typeof rows[0].id !== 'string' || !rows[0].id)
  throw new Error('Native session is not strict v3');
// In-memory loader is intentional: SessionManager.open() has an empty-file
// initialization writer if the path races. Preparation must NEVER own a writer.
const manager = SessionManager.inMemory(process.cwd(), undefined, rows);
if (manager.getSessionId() !== rows[0].id || !manager.getLeafId())
  throw new Error('Native session identity changed');
const stat = lstatSync(file, {bigint:true});
if (!stat.isFile() || stat.nlink !== 1n || stat.size > 256n*1024n*1024n)
  throw new Error('Native session revision unavailable');
// Mirrors the exact pinned pr48DiskRevision tuple. Writer CAS independently
// recomputes it under the session lock; this is evidence, never authority.
const revision = [stat.dev,stat.ino,stat.size,stat.mtimeNs,stat.ctimeNs]
  .map(String).join(':');
const preparation = prepareCompaction(manager.getBranch(), settings ?? DEFAULT_COMPACTION_SETTINGS);
if (!preparation) {
  console.log(JSON.stringify({status:'skip', sessionId:rows[0].id}));
} else {
  if (!manager.getBranch().some(entry => entry.id === preparation.firstKeptEntryId))
    throw new Error('Native kept entry changed');
  const witness = {sessionId:manager.getSessionId(),sessionFile:realpathSync(file),
    leafId:manager.getLeafId(),firstKeptEntryId:preparation.firstKeptEntryId,revision};
  console.log(JSON.stringify({status:'ready',
    witness, tokensBefore:preparation.tokensBefore,
    isSplitTurn:preparation.isSplitTurn}));
}
