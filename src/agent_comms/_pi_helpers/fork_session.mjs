import {readFileSync as readHelperInput} from "node:fs";
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {package:root, file, cwd, directory} = JSON.parse(readHelperInput(0, 'utf8'));
const {SessionManager} = await import(pathToFileURL(join(root, 'dist/core/session-manager.js')));
const child = SessionManager.forkFrom(file, cwd, directory ?? undefined);
try {
  child.entryStore.assertCurrent();
  console.log(JSON.stringify({sessionId:child.getSessionId(), sessionFile:child.getSessionFile()}));
} finally {child.entryStore.close();}
