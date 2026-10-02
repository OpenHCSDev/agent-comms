import {readFileSync as readHelperInput} from "node:fs";
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {package:root, file, cwd, directory, creation_kind} = JSON.parse(readHelperInput(0, 'utf8'));
const {SessionManager} = await import(pathToFileURL(join(root, 'dist/core/session-manager.js')));
const child = SessionManager.forkFrom(file, cwd, directory ?? undefined, {
  // The SDK owns both locks and fsync before this single creation observation.
  onForkCreated(creation) { console.log(JSON.stringify({kind: creation_kind, ...creation})); },
});
try {
  child.entryStore.assertCurrent();
} finally {child.entryStore.close();}
