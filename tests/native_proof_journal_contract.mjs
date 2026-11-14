/** What the proof journal records while requests keep holding the same inputs. */
import {mkdirSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

const [pkg, root] = process.argv.slice(2);
const {NativeProof} = await import(pathToFileURL(join(pkg, 'dist/core/native-proof-journal.js')));
mkdirSync(root, {recursive: true, mode: 0o700});
const file = join(root, 'proof.sqlite');
const tracked = names => names.map(name => ({inputId: name.repeat(32), sessionEntryId: `entry-${name}`}));
const requests = [];
const request = (journal, names) => requests.push(
    journal.commit(tracked(names), 'd'.repeat(64)).map(record => record.inputId[0]));

const first = NativeProof.fromPath(file, 'session');
request(first, ['a']);
request(first, ['a', 'b']);
request(first, ['a', 'b']);
// A process that reopened the session knows nothing yet, so it asks the journal.
const reopened = NativeProof.fromPath(file, 'session');
request(reopened, ['a', 'b', 'c']);
request(reopened, ['a', 'b', 'c']);
console.log(JSON.stringify({file, requests}));
