import {readFileSync as readHelperInput, createReadStream, realpathSync} from 'node:fs';
import {createInterface} from 'node:readline';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

const {package:root, file} = JSON.parse(readHelperInput(0, 'utf8'));
const {EntryStore} = await import(pathToFileURL(join(root, 'dist/core/session-entry-store.js')));
const source = createReadStream(file, {encoding:'latin1'});
const lines = createInterface({input:source, crlfDelay:Infinity});
try {
    for await (const line of lines) {
        const header = EntryStore.validateHeader(JSON.parse(
            new TextDecoder('utf-8', {fatal:true}).decode(Buffer.from(line, 'latin1'))
        ));
        process.stdout.write(JSON.stringify({sessionId:header.id, sessionFile:realpathSync(file)}));
        break;
    }
} finally {
    lines.close();
    source.destroy();
}
