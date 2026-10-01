import {realpathSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {package:root, file, settings, context_window} = JSON.parse(process.argv[1]);
const {DiskEntryStore} = await import(
  pathToFileURL(join(root, 'dist/core/session-entry-store.js')));
const {prepareCompaction} = await import(
  pathToFileURL(join(root, 'dist/core/compaction/compaction.js')));
const store = new DiskEntryStore(file);
try {
const revision = store.revision;
const preparation = prepareCompaction(store, settings,
    {contextWindow: context_window});
if (!preparation) {
  console.log(JSON.stringify({status:'skip', sessionId:store.header.id}));
} else {
  if (!store.branchContains(store.lastId, preparation.firstKeptEntryId))
    throw new Error('Native kept entry changed');
  const witness = {sessionId:store.header.id,sessionFile:realpathSync(file),
    leafId:store.lastId,firstKeptEntryId:preparation.firstKeptEntryId,revision};
  console.log(JSON.stringify({status:'ready',
    witness, tokensBefore:preparation.tokensBefore,
    isSplitTurn:preparation.isSplitTurn}));
}
store.assertCurrent();
} finally {store.close();}
