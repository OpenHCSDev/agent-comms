/** Which entries the pinned package's journal store calls durable, around one sync. */
import {appendFileSync, mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

const [pkg, root] = process.argv.slice(2);
const {DiskEntryStore, MemoryEntryStore} = await import(pathToFileURL(join(pkg, 'dist/core/session-entry-store.js')));
mkdirSync(root, {recursive: true, mode: 0o700});
const file = join(root, 'session.jsonl');
const header = {type: 'session', version: 3, id: 'durability', cwd: root};
const input = (id, parentId) => ({type: 'message', id, parentId,
    message: {role: 'user', content: id, inputId: `input-${id}`, inputDigest: 'digest'}});
const first = input('a', null), second = input('b', 'a');
writeFileSync(file, [header, first].map(row => JSON.stringify(row) + '\n').join(''));
const disk = new DiskEntryStore(file, {indexDirectory: root});
const memory = new MemoryEntryStore(header, [first]);
const durable = (store, id) => store.durable(store.trackedInputMetadata(`input-${id}`));
const seen = {memory: durable(memory, 'a'), opened: durable(disk, 'a')};
seen.same_store = disk.syncedAt(file) === disk;
seen.synced = durable(disk, 'a');
appendFileSync(file, JSON.stringify(second) + '\n');
disk.committedAppend(second, file);
seen.appended_after = durable(disk, 'b');
seen.still = durable(disk, 'a');
disk.syncedAt(file);
seen.synced_again = durable(disk, 'b');
disk.close(); memory.close();
console.log(JSON.stringify(seen));
