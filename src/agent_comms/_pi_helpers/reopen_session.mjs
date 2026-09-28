import {realpathSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {package:root, file} = JSON.parse(process.argv[1]);
const {DiskEntryStore} = await import(pathToFileURL(join(root, 'dist/core/session-entry-store.js')));
const store = new DiskEntryStore(file);
try {
  store.assertCurrent();
  console.log(JSON.stringify({sessionId:store.header.id, sessionFile:realpathSync(file)}));
} finally {store.close();}
