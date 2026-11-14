/** How often the pinned package reads a context's journal entries from the session file. */
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

const [pkg, root] = process.argv.slice(2);
const load = name => import(pathToFileURL(join(pkg, `dist/core/${name}.js`)));
const {SessionManager} = await load('session-manager');
const {ContextEntries} = await load('turn-context');
mkdirSync(root, {recursive: true, mode: 0o700});
const file = join(root, 'session.jsonl');
const header = {type: 'session', version: 3, id: 'entries', cwd: root, timestamp: new Date(0).toISOString()};
const message = (id, parentId, role) => ({type: 'message', id, parentId, timestamp: new Date(0).toISOString(),
    message: role === 'user' ? {role, content: id, timestamp: 0}
        : {role, content: [{type: 'text', text: id}], api: 'fixture', provider: 'fixture', model: 'fixture',
            usage: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
                cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0}},
            stopReason: 'stop', timestamp: 0}});
const rows = [message('a', null, 'user'), message('b', 'a', 'assistant'), message('c', 'b', 'user')];
writeFileSync(file, [header, ...rows].map(row => JSON.stringify(row) + '\n').join(''), {mode: 0o600});
const manager = SessionManager.open(file, root);
const store = manager.entryStore, get = store.get.bind(store);
let reads = 0;
store.get = id => { reads += 1; return get(id); };
const ids = () => ContextEntries.of(manager).map(entry => entry.id);
const seen = {first: ids(), first_reads: reads};
seen.again = ids(); seen.again_reads = reads;
const appended = manager.appendMessage({role: 'user', content: 'd', timestamp: 0});
seen.after_append = ids().map(id => id === appended ? 'appended' : id); seen.after_append_reads = reads;
seen.same_as_the_store = JSON.stringify(ContextEntries.of(manager)) === JSON.stringify(Array.from(manager.buildContextEntries()));
console.log(JSON.stringify(seen));
process.exit(0);
