import {realpathSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {package:root, file} = JSON.parse(process.argv[1]);
const managerURL = pathToFileURL(join(root, 'dist/core/session-manager.js'));
const {loadEntriesFromFile} = await import(managerURL);
const rows = loadEntriesFromFile(file);
if (!rows.length || rows[0].type !== 'session' || rows[0].version !== 3 ||
    typeof rows[0].id !== 'string' || !rows[0].id)
    throw new Error('Exact saved native session identity unavailable');
console.log(JSON.stringify({sessionId:rows[0].id, sessionFile:realpathSync(file)}));
