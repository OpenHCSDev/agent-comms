import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {package:root, file, cwd} = JSON.parse(process.argv[1]);
const {SessionManager} = await import(pathToFileURL(join(root, 'dist/core/session-manager.js')));
const child = SessionManager.forkFrom(file, cwd);
try {
  child.entryStore.assertCurrent();
  console.log(JSON.stringify({sessionId:child.getSessionId(), sessionFile:child.getSessionFile()}));
} finally {child.entryStore.close();}
