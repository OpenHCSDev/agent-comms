import {readFileSync, statSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

const {package:root, file} = JSON.parse(readFileSync(0, 'utf8'));
const {ReadOnlyEntryStore} = await import(pathToFileURL(join(root, 'dist/core/session-entry-store.js')));
const store = new ReadOnlyEntryStore(file);
try {
    const selected = store.latestToolMetadata();
    const observed = statSync(file, {bigint:true});
    store.assertCurrent();
    // Only original coordinates cross this boundary. Python reads the exact
    // JSONL bytes itself: JS Number must not round large integer tool arguments.
    const result = {
        revision: {
            identity:{device:observed.dev, inode:observed.ino}, size:observed.size,
            mtime_ns:observed.mtimeNs, ctime_ns:observed.ctimeNs,
        },
        latest_tool: selected ? {
            entry_id:selected.id, start:selected.offset, end:selected.offset + selected.length + 1,
        } : null,
    };
    process.stdout.write(JSON.stringify(result, (_, value) =>
        typeof value === 'bigint' ? JSON.rawJSON(value.toString()) : value));
} finally { store.close(); }
